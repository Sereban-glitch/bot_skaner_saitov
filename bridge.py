#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import aiohttp
import contextlib
import fcntl
import html
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl.types import MessageEntityBotCommand, MessageMediaWebPage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_PATH = os.path.join(BASE_DIR, "processed_ids.json")

def truncate_caption(caption: str | None, limit: int = 1024) -> str | None:
    if caption and len(caption) > limit:
        return caption[:limit]
    return caption

_processed_ids_cache: list[int] | None = None

def load_processed_ids() -> list[int]:
    global _processed_ids_cache
    if _processed_ids_cache is not None:
        return _processed_ids_cache
    
    if not os.path.exists(PROCESSED_PATH):
        _processed_ids_cache = []
        return []
    try:
        with open(PROCESSED_PATH, "r") as f: 
            data = json.load(f)
            _processed_ids_cache = data if isinstance(data, list) else []
            return _processed_ids_cache
    except Exception:
        _processed_ids_cache = []
        return []

def commit_processed_ids():
    global _processed_ids_cache
    if _processed_ids_cache is None:
        return
    
    # Лимит 2000, храним как очередь
    _processed_ids_cache = _processed_ids_cache[-2000:]
    tmp_path = PROCESSED_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(_processed_ids_cache, f)
        os.replace(tmp_path, PROCESSED_PATH)
    except Exception as e:
        print(f"Error saving processed_ids: {e}", flush=True)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def save_processed_id(msg_id: int, commit: bool = True):
    ids = load_processed_ids()
    if msg_id not in ids:
        ids.append(msg_id)
        if commit:
            commit_processed_ids()


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: str = "1") -> bool:
    return env(name, default).lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: str = "0") -> int:
    value = env(name, default)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return int(value)


def env_float(name: str, default: str = "0") -> float:
    value = env(name, default)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return float(value)


def env_list(name: str) -> list[str]:
    raw = env(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_chat_id(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw)
    if raw.isdigit():
        return int(raw)
    return None


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    source_chat: str
    target_channel: str
    source_author_id: str
    append_source_link: bool
    source_link_label: str
    notify_chat: str
    notify_group_chats: tuple[str, ...]
    notify_rss_chats: tuple[str, ...]
    album_wait_seconds: float
    rss_feed_url: str
    rss_target_channel: str
    rss_poll_seconds: float
    rss_state_path: str
    rss_recent_limit: int


@dataclass(frozen=True)
class StatePaths:
    directory: str
    source: str
    rss: str
    health: str
    lock: str

    @classmethod
    def from_directory(cls, directory: str) -> "StatePaths":
        root = os.path.abspath(directory)
        return cls(
            directory=root,
            source=os.path.join(root, "bridge.state.json"),
            rss=os.path.join(root, "rss-state.json"),
            health=os.path.join(root, "bridge.health.json"),
            lock=os.path.join(root, "bridge.lock"),
        )

    @classmethod
    def from_environment(cls) -> "StatePaths":
        return cls.from_directory(env("BRIDGE_STATE_DIR", BASE_DIR))


@dataclass(frozen=True)
class OnceOptions:
    seed_current: bool = False
    force_reseed: bool = False
    shadow: bool = False
    max_source_items: int = 20
    max_rss_items: int = 5
    deadline_seconds: float = 780


@dataclass
class OnceResult:
    source_inspected: int = 0
    source_filtered: int = 0
    source_selected: int = 0
    source_sent: int = 0
    rss_selected: int = 0
    rss_sent: int = 0
    notification_failures: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "source_inspected": self.source_inspected,
            "source_filtered": self.source_filtered,
            "source_selected": self.source_selected,
            "source_sent": self.source_sent,
            "rss_selected": self.rss_selected,
            "rss_sent": self.rss_sent,
            "notification_failures": self.notification_failures,
        }


class DurableStateError(RuntimeError):
    pass


class PrimarySendError(RuntimeError):
    pass


class RSSFeedError(RuntimeError):
    pass


class LockBusyError(RuntimeError):
    pass


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        api_id=env_int("API_ID"),
        api_hash=env("API_HASH"),
        session_name=env("SESSION_NAME", ".mtproto-session"),
        source_chat=env("SOURCE_CHAT"),
        target_channel=env("TARGET_CHANNEL"),
        source_author_id=env("SOURCE_AUTHOR_ID"),
        append_source_link=env_bool("APPEND_SOURCE_LINK", "1"),
        source_link_label=env("SOURCE_LINK_LABEL", "@source_group"),
        notify_chat=env("NOTIFY_CHAT"),
        notify_group_chats=tuple(env_list("NOTIFY_GROUP_CHATS")),
        notify_rss_chats=tuple(env_list("NOTIFY_RSS_CHATS")),
        album_wait_seconds=env_float("ALBUM_WAIT_SECONDS", "1.5"),
        rss_feed_url=env("RSS_FEED_URL"),
        rss_target_channel=env("RSS_TARGET_CHANNEL"),
        rss_poll_seconds=env_float("RSS_POLL_SECONDS", "300"),
        rss_state_path=env("RSS_STATE_PATH", ".rss-state.json"),
        rss_recent_limit=env_int("RSS_RECENT_LIMIT", "30"),
    )


def require_nonempty(value: str, name: str) -> str:
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def build_footer(settings: Settings) -> str:
    if not settings.append_source_link:
        return ""
    return f"\n\nИсточник: {settings.source_link_label}"


def merge_text(original: str | None, footer: str) -> str:
    base = original or ""
    if not footer:
        return base
    return f"{base}{footer}" if base else footer.strip()


def parse_author_id(settings: Settings) -> int | None:
    raw = settings.source_author_id
    if not raw:
        return None
    return int(raw)


def is_command_message(message: Any) -> bool:
    entities = getattr(message, "entities", None)
    if not entities:
        return False
    for entity in entities:
        if isinstance(entity, MessageEntityBotCommand) and getattr(entity, "offset", None) == 0:
            return True
    return False


def has_media(message: Any) -> bool:
    if isinstance(getattr(message, "media", None), MessageMediaWebPage):
        return False
    return any(
        getattr(message, attr, None)
        for attr in ("photo", "video", "animation", "document", "audio", "voice")
    )


def should_skip_message(settings: Settings, message: Any) -> bool:
    if getattr(message, "service", False):
        return True
    if is_command_message(message):
        return True
    if getattr(message, "from_id", None) is None and getattr(message, "sender_id", None) is None:
        return True
    source_author_id = parse_author_id(settings)
    if source_author_id is not None:
        sender_id = getattr(message, "sender_id", None)
        if sender_id != source_author_id:
            return True
    
    has_content = bool(getattr(message, "raw_text", None) or getattr(message, "media", None))
    return not has_content


def media_kind(message: Any) -> str | None:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "voice", None):
        return "voice"
    return None


def load_state(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def single_instance(path: str, write_pid: bool = True, allow_missing: bool = False):
    directory = os.path.dirname(path)
    if directory and write_pid:
        os.makedirs(directory, exist_ok=True)
    if not write_pid and not os.path.exists(path):
        if allow_missing:
            yield
            return
        raise DurableStateError(f"shared lock does not exist: {path}")
    mode = "a+" if write_pid else "r+"
    handle = open(path, mode, encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusyError(f"Bridge is already running or lock is busy: {path}") from exc
        locked = True
        if write_pid:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()


def strip_html(raw: str | None) -> str:
    text = raw or ""
    # CRITICAL: Remove script/style/nav/header/footer blocks BEFORE stripping tags
    # Otherwise JS code leaks into the text and AI hallucinates
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<header[^>]*>.*?</header>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # Now strip remaining HTML tags
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\[\s*(?:…|\.{3})\s*\]$", "", text)
    text = text.replace("[…]", "").replace("[...]", "")
    return text.strip()


def extract_article_body(html_content: str) -> str:
    """Extract main article content from HTML.
    Tries (in order):
      1. <article> tag
      2. WordPress entry-content div
      3. <main> tag
      4. Falls back to strip_html on full page
    Returns cleaned text.
    """
    # Try <article> first (most semantic)
    match = re.search(r"<article[^>]*>(.*?)</article>", html_content, re.DOTALL | re.IGNORECASE)
    if match:
        body = strip_html(match.group(1))
        if len(body) > 500:  # sanity check
            return body
    # Try WordPress entry-content
    match = re.search(r'class=["\']entry-content["\'][^>]*>(.*?)</div>\s*(?:</div>|<footer)', html_content, re.DOTALL | re.IGNORECASE)
    if match:
        body = strip_html(match.group(1))
        if len(body) > 500:
            return body
    # Try <main>
    match = re.search(r"<main[^>]*>(.*?)</main>", html_content, re.DOTALL | re.IGNORECASE)
    if match:
        body = strip_html(match.group(1))
        if len(body) > 500:
            return body
    # Fallback: full page with improved strip_html
    return strip_html(html_content)


def shorten(text: str, limit: int = 500) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


async def fetch_url_text(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0 Codex Bridge"}, timeout=30) as response:
                return await response.text()
    except Exception as e:
        print(f"fetch_url_text error: {e}")
        return ""

# Regex for Ukrainian court case numbers
CASE_NUMBER_PATTERNS = [
    r'(?:дело|справа|производство|постановление|рішення)\s*№?\s*(\d+/\d+/\d+(?:/\d+)?)',
    r'№\s*(\d{3,}/\d{4,}/\d{2,})',
    r'(\d{3}/\d{4,}/\d{2,})',
    r'(№\s*\d+[а-яА-Я]?/\d+/\d+)',
]


def extract_case_numbers(text: str) -> list[str]:
    """Extract all case numbers from text. Returns list of unique matches."""
    found = set()
    for pattern in CASE_NUMBER_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            num = match.group(1).strip()
            if num and len(num) > 5:
                found.add(num)
    return sorted(found)


def case_number_in_post(case_numbers: list[str], post_text: str) -> bool:
    """Check if at least one case number is mentioned in the post."""
    if not case_numbers:
        return True
    post_lower = post_text.lower()
    for num in case_numbers:
        if num in post_lower:
            return True
    return False


async def analyze_with_ai(html_content: str) -> str:
    # Use extract_article_body to skip JS/CSS/nav and get real article content
    text = extract_article_body(html_content)
    if len(text) < 200:
        return "FALLBACK"
    
    # Extract case numbers from source text BEFORE AI processing
    source_case_numbers = extract_case_numbers(text)
    case_numbers_str = ", ".join(source_case_numbers) if source_case_numbers else "не найден в источнике"
    prompt = f"""Ты — профессиональный Telegram-редактор и аналитик.

ТВОЯ ЗАДАЧА — сделать из текста крутой, профессиональный пост-выжимку.

🛑 ВАЖНОЕ ПРАВИЛО (ЦЕНЗУРА):
Автор может резко отзываться о властях, использовать нецензурную лексику или агрессию.
Ты ОБЯЗАН ПОЛНОСТЬЮ ЭТО ОТФИЛЬТРОВАТЬ:
- Смягчи тон до нейтрального и объективного.
- Смести акценты ИСКЛЮЧИТЕЛЬНО на суть прецедента, интересные факты и судебную практику.
- Убери любую политическую окраску и ругань.

📂 ОБЯЗАТЕЛЬНО: НОМЕР ДЕЛА
Номер судебного дела — это маркер достоверности. Без него пост выглядит как слух.
Найденные в источнике номера дел: {case_numbers_str}

ПРАВИЛА:
- Если номер дела есть в источнике — ОБЯЗАТЕЛЬНО включи его в пост отдельной строкой:
  📂 Номер дела: №753/19985/25
- Если номер дела не найден — напиши:
  📂 Номер дела: не указан в источнике
- Никогда не выдумывай номер дела. Только из источника.

🚫 АНТИ-ГАЛЛЮЦИНАЦИЯ (КРИТИЧНО):
- ИСПОЛЬЗУЙ ТОЛЬКО факты из предоставленного текста. Не выдумывай ничего.
- Если текст не понятен или не содержит статьи — верни слово FALLBACK.
- Не додумывай содержание по заголовку. Заголовок может быть двусмысленным.
- Если в тексте идёт речь о судебном решении — пиши про судебное решение, а не про «деятельность» или «юбилей».
- Если не уверен в факте — не включай его. Лучше короче, но точно.

📝 СТРУКТУРА ПОСТА (Метод обратной пирамиды):
1. Заголовок (цепляющий, отражающий суть новости/прецедента).
2. Лид (самое важное в 1-2 предложениях).
3. 📂 Номер дела (ОБЯЗАТЕЛЬНО — отдельной строкой).
4. Детали (интересные факты, практика).
5. Значение (почему это важно читателю).

ОФОРМЛЕНИЕ:
Используй абзацы, маркированные списки и уместные эмодзи (⚖️, 📌, 📜, 🏛️, 📊, 📂).
Не пиши приветствий, выводов или заключений от себя.

Текст:
""" + text[:8000]

    try:
        async with aiohttp.ClientSession() as session:
            # IMPORTANT: proxy at :18080 is antigravity-claude-proxy (Anthropic API format)
            # NOT OpenAI. Use /v1/messages with top-level "system" field.
            payload = {
                "model": "gemini-3.7-flash-tiered",
                "max_tokens": 4000,
                "system": "Ты — профессиональный Telegram-редактор и аналитик. Твоя задача — делать крутые посты-выжимки, фильтровать агрессию, смещать акценты на суть прецедента. Тон нейтральный и объективный. ВСЕГДА включай номер дела отдельной строкой.",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            }
            headers = {"Content-Type": "application/json", "x-api-key": "test"}
            async with session.post("http://127.0.0.1:18080/v1/messages", json=payload, headers=headers, timeout=45) as resp:
                data = await resp.json()
                # Anthropic format: data["content"] = [{"type": "text", "text": "..."}, ...]
                # Skip "thinking" blocks, take first "text" block
                res = ""
                if "content" in data and isinstance(data["content"], list):
                    for block in data["content"]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            res = block.get("text", "").strip()
                            break
                if not res:
                    return "FALLBACK"
                # Защита от системных сообщений Antigravity
                if "is no longer available" in res or "switch to Gemini" in res or "Antigravity" in res:
                    print(f"API error injected in text: {res}", flush=True)
                    return "FALLBACK"
                if "FALLBACK" in res.upper() and len(res) < 20:
                    return "FALLBACK"
                # Validate: if source had case numbers, they MUST be in the post
                if source_case_numbers and not case_number_in_post(source_case_numbers, res):
                    case_line = "📂 Номер дела: " + ", ".join("№" + n for n in source_case_numbers)
                    lines = res.split("\n")
                    if len(lines) > 1:
                        lines.insert(1, case_line)
                    else:
                        lines.append(case_line)
                    res = "\n".join(lines)
                    print(f"AI dropped case number — added manually: {source_case_numbers}", flush=True)
                return res
    except Exception as e:
        print(f"AI error: {e}", flush=True)
    return "FALLBACK"


def extract_article_excerpt(article_html: str) -> str:
    for pattern in [
        r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']',
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
    ]:
        match = re.search(pattern, article_html, re.I | re.S)
        if match:
            text = strip_html(match.group(1))
            if text:
                return text

    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", article_html, re.I | re.S)
    chunks: list[str] = []
    for paragraph in paragraphs:
        text = strip_html(paragraph)
        if not text or len(text) < 40:
            continue
        chunks.append(text)
        if len(" ".join(chunks)) >= 450:
            break
    return shorten(" ".join(chunks), 650)


async def build_rss_excerpt(description: str, content: str, link: str) -> str:
    excerpt = description or content
    try:
        article_html = await fetch_url_text(link)
    except Exception:
        return shorten(excerpt, 650)
        
    if article_html:
        ai_result = await analyze_with_ai(article_html)
        if ai_result != "FALLBACK":
            return ai_result
            
    html_excerpt = extract_article_excerpt(article_html)
    return shorten(html_excerpt or excerpt, 650)


async def fetch_url_text_strict(url: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers={"User-Agent": "Mozilla/5.0 Codex Bridge"}, timeout=30
        ) as response:
            response.raise_for_status()
            return await response.text()


async def parse_rss_metadata_items(feed_url: str, fetcher: Any = None) -> list[dict[str, str]]:
    fetch = fetcher or fetch_url_text_strict
    try:
        payload = await fetch(feed_url)
    except RSSFeedError:
        raise
    except Exception as exc:
        raise RSSFeedError(f"RSS fetch failed: {exc}") from exc
    if not isinstance(payload, str) or not payload.strip():
        raise RSSFeedError("RSS feed returned an empty response")
    try:
        root = ET.fromstring(payload.encode("utf-8"))
    except (ET.ParseError, UnicodeError) as exc:
        raise RSSFeedError(f"malformed RSS XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise RSSFeedError("malformed RSS XML: channel is missing")

    items: list[dict[str, str]] = []
    for feed_order, item in enumerate(channel.findall("item")):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        description = strip_html(item.findtext("description"))
        content = ""
        for child in item:
            if child.tag.endswith("encoded"):
                content = strip_html(child.text)
                break
            if not published and child.tag.endswith("date"):
                published = (child.text or "").strip()
        item_id = guid or link or title
        if not item_id or not link:
            continue

        # Keep original title from feed. format_rss_post() will check
        # if excerpt starts with the title (AI-generated) to avoid duplication.
        items.append(
            {
                "id": item_id,
                "guid": guid,
                "title": title,
                "link": link,
                "description": description,
                "content": content,
                "published": published,
                "feed_order": str(feed_order),
            }
        )
    return items


async def parse_rss_items(feed_url: str) -> list[dict[str, str]]:
    items = await parse_rss_metadata_items(feed_url)
    for item in items:
        item["excerpt"] = await build_rss_excerpt(
            item["description"], item["content"], item["link"]
        )
    return items


def format_rss_post(item: dict[str, str]) -> str:
    parts = []
    title = item.get("title", "").strip()
    excerpt = item.get("excerpt", "").strip()
    # If excerpt already starts with title (AI-generated post), skip duplicating title
    if title and not (excerpt and excerpt.startswith(title)):
        parts.append(title)
    if excerpt:
        parts.append(excerpt)
    parts.append(f"Источник: {item['link']}")
    return "\n\n".join(part for part in parts if part)


def item_keys(item: dict[str, str]) -> list[str]:
    keys: list[str] = []
    for key in (item.get("id", ""), item.get("guid", ""), item.get("link", "")):
        value = key.strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram MTProto bridge")
    parser.add_argument("--once", action="store_true", help="run one safe catch-up")
    parser.add_argument("--seed-current", action="store_true", help="seed current baselines")
    parser.add_argument("--force-reseed", action="store_true", help="replace existing baselines")
    parser.add_argument("--shadow", action="store_true", help="select and format without sends or state writes")
    parser.add_argument("--max-source-items", type=int, default=20)
    parser.add_argument("--max-rss-items", type=int, default=5)
    parser.add_argument("--deadline-seconds", type=float, default=780)
    args = parser.parse_args(argv)
    if args.force_reseed and not args.seed_current:
        parser.error("--force-reseed requires --seed-current")
    if args.seed_current and args.shadow:
        parser.error("--seed-current and --shadow cannot be combined")
    if args.max_source_items < 1 or args.max_rss_items < 1 or args.deadline_seconds <= 0:
        parser.error("item limits and deadline must be positive")
    return args


def _read_json_required(path: str, label: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise DurableStateError(f"missing {label}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DurableStateError(f"malformed {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise DurableStateError(f"malformed {label}: expected JSON object")
    return payload


def _source_state(settings: Settings, path: str) -> dict[str, Any]:
    state = _read_json_required(path, "source baseline")
    expected = (1, settings.source_chat, settings.target_channel)
    actual = (state.get("schema"), state.get("source_chat"), state.get("target_channel"))
    cursor = state.get("last_source_message_id")
    if actual != expected or not isinstance(cursor, int) or cursor < 0:
        raise DurableStateError("source baseline is malformed or does not match configuration")
    return state


def _rss_state(settings: Settings, path: str) -> dict[str, Any]:
    state = _read_json_required(path, "RSS baseline")
    expected = (1, settings.rss_feed_url, settings.rss_target_channel)
    actual = (state.get("schema"), state.get("feed_url"), state.get("target_channel"))
    recent_ids = state.get("recent_ids")
    if (
        actual != expected
        or not isinstance(state.get("last_seen_id"), str)
        or not state.get("last_seen_id", "").strip()
        or not isinstance(recent_ids, list)
        or not recent_ids
        or any(not isinstance(value, str) or not value.strip() for value in recent_ids)
    ):
        raise DurableStateError("RSS baseline is malformed or does not match configuration")
    return state


def _source_payload(settings: Settings, cursor: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "source_chat": settings.source_chat,
        "target_channel": settings.target_channel,
        "last_source_message_id": cursor,
    }


def _rss_payload(settings: Settings, last_seen_id: str, recent_ids: list[str]) -> dict[str, Any]:
    return {
        "schema": 1,
        "feed_url": settings.rss_feed_url,
        "target_channel": settings.rss_target_channel,
        "last_seen_id": last_seen_id,
        "recent_ids": recent_ids,
    }


def load_or_migrate_source_state(settings: Settings, paths: StatePaths) -> dict[str, Any]:
    if os.path.exists(paths.source):
        return _source_state(settings, paths.source)
    health = _read_json_required(paths.health, "source baseline; run --seed-current")
    expected = (settings.source_chat, settings.target_channel)
    actual = (health.get("source_chat"), health.get("target_channel"))
    cursor = health.get("last_source_message_id")
    if actual != expected or not isinstance(cursor, int) or cursor < 0:
        raise DurableStateError("no valid matching source cursor; run --seed-current explicitly")
    state = _source_payload(settings, cursor)
    save_state(paths.source, state)
    return state


async def publish_primary(
    sender: Any,
    *,
    checkpoint: Any,
    notify: Any = None,
    label: str = "primary",
) -> Any:
    result = await sender()
    if result is None:
        raise PrimarySendError(f"{label} send returned no result")
    checkpoint()
    if notify is not None:
        with contextlib.suppress(Exception):
            await notify()
    return result


def _snapshot_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _restore_file(path: str, snapshot: bytes | None) -> None:
    if snapshot is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.restore.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)


def _write_seed_transaction(
    paths: StatePaths,
    source_payload: dict[str, Any],
    rss_payload: dict[str, Any] | None,
    writer: Any,
) -> None:
    snapshots = {paths.source: _snapshot_file(paths.source), paths.rss: _snapshot_file(paths.rss)}
    try:
        writer(paths.source, source_payload)
        if rss_payload is not None:
            writer(paths.rss, rss_payload)
    except Exception:
        _restore_file(paths.source, snapshots[paths.source])
        _restore_file(paths.rss, snapshots[paths.rss])
        raise


def _health_payload(
    status: str,
    result: OnceResult,
    started_at: str,
    error: str = "",
    finished: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "pid": os.getpid(),
        "started_at": started_at,
        "updated_at": utc_now(),
        "counts": result.counts(),
        "error": error,
    }
    if finished:
        payload["finished_at"] = utc_now()
    return payload


async def _default_rss_post_builder(item: dict[str, str]) -> str:
    enriched = dict(item)
    enriched["excerpt"] = await build_rss_excerpt(
        item.get("description", ""), item.get("content", ""), item["link"]
    )
    return format_rss_post(enriched)


async def _collect_source_messages(client: Any, entity: Any, cursor: int, maximum: int) -> list[Any]:
    messages: list[Any] = []
    trailing_group = None
    async for message in client.iter_messages(entity, min_id=cursor, reverse=True):
        grouped_id = getattr(message, "grouped_id", None)
        if len(messages) >= maximum and not (trailing_group and grouped_id == trailing_group):
            break
        messages.append(message)
        if len(messages) == maximum:
            trailing_group = grouped_id
    return messages


def _message_batches(messages: list[Any]) -> list[list[Any]]:
    batches: list[list[Any]] = []
    index = 0
    while index < len(messages):
        first = messages[index]
        grouped_id = getattr(first, "grouped_id", None)
        if not grouped_id:
            batches.append([first])
            index += 1
            continue
        end = index + 1
        while end < len(messages) and getattr(messages[end], "grouped_id", None) == grouped_id:
            end += 1
        batches.append(messages[index:end])
        index = end
    return batches


def _select_unseen_rss(
    items: list[dict[str, str]], seen: set[str], maximum: int
) -> list[dict[str, str]]:
    unseen = [item for item in items if not any(key in seen for key in item_keys(item))]
    dated: list[tuple[float, int, dict[str, str]]] = []
    for index, item in enumerate(unseen):
        try:
            timestamp = parsedate_to_datetime(item.get("published", "")).timestamp()
        except (TypeError, ValueError, OverflowError):
            break
        dated.append((timestamp, index, item))
    if len(dated) == len(unseen):
        unseen = [item for _, _, item in sorted(dated, key=lambda value: (value[0], value[1]))]
    else:
        unseen.reverse()
    return unseen[:maximum]


async def _notify_best_effort(
    client: Any,
    settings: Settings,
    chats: tuple[str, ...],
    text: str,
    result: OnceResult,
    output: Any,
) -> None:
    recipients = [chat for chat in chats if chat]
    if not recipients and settings.notify_chat:
        recipients = [settings.notify_chat]
    for chat in recipients:
        try:
            sent = await client.send_message(chat, text)
            if sent is None:
                raise RuntimeError("send returned no result")
        except Exception as exc:
            result.notification_failures += 1
            output(f"notification failed destination={chat} error={exc}")


async def _seed_current(
    settings: Settings,
    client: Any,
    options: OnceOptions,
    paths: StatePaths,
    rss_items_loader: Any,
    state_writer: Any,
) -> OnceResult:
    if not options.force_reseed and (
        os.path.exists(paths.source)
        or (settings.rss_feed_url and settings.rss_target_channel and os.path.exists(paths.rss))
    ):
        raise DurableStateError("baseline already exists; use --force-reseed to replace it")

    await client.start()
    entity = await client.get_entity(normalize_chat_id(settings.source_chat) or settings.source_chat)
    newest_id = None
    async for message in client.iter_messages(entity, limit=1):
        newest_id = int(message.id)
        break
    if newest_id is None:
        raise DurableStateError("source has no messages to seed")

    rss_items: list[dict[str, str]] = []
    if settings.rss_feed_url and settings.rss_target_channel:
        rss_items = await rss_items_loader(settings.rss_feed_url)
        if not rss_items:
            raise DurableStateError("RSS feed has no items to seed")

    rss_payload = None
    if rss_items:
        recent_ids: list[str] = []
        for item in rss_items:
            for key in item_keys(item):
                if key not in recent_ids:
                    recent_ids.append(key)
                if len(recent_ids) >= settings.rss_recent_limit:
                    break
            if len(recent_ids) >= settings.rss_recent_limit:
                break
        rss_payload = _rss_payload(settings, rss_items[0]["id"], recent_ids)
    _write_seed_transaction(
        paths, _source_payload(settings, newest_id), rss_payload, state_writer
    )
    return OnceResult()


async def _run_catch_up(
    settings: Settings,
    client: Any,
    options: OnceOptions,
    paths: StatePaths,
    rss_items_loader: Any,
    rss_post_builder: Any,
    output: Any,
    result: OnceResult,
) -> OnceResult:
    if options.shadow and not os.path.exists(paths.source):
        source_state = _source_payload(settings, 0)
    else:
        source_state = _source_state(settings, paths.source)
    rss_state = None
    if settings.rss_feed_url and settings.rss_target_channel:
        if options.shadow and not os.path.exists(paths.rss):
            rss_state = _rss_payload(settings, "", [])
        else:
            rss_state = _rss_state(settings, paths.rss)

    await client.start()
    entity = await client.get_entity(normalize_chat_id(settings.source_chat) or settings.source_chat)
    cursor = source_state["last_source_message_id"]
    messages = await _collect_source_messages(
        client, entity, cursor, options.max_source_items
    )
    for batch in _message_batches(messages):
        result.source_inspected += len(batch)
        eligible = [message for message in batch if not should_skip_message(settings, message)]
        checkpoint = max(int(message.id) for message in batch)
        if not eligible:
            result.source_filtered += len(batch)
            if not options.shadow:
                source_state["last_source_message_id"] = checkpoint
                save_state(paths.source, source_state)
            continue

        result.source_filtered += len(batch) - len(eligible)
        result.source_selected += 1
        ids = ",".join(str(message.id) for message in eligible)
        output(f"source id={ids} destination={settings.target_channel}")
        if options.shadow:
            continue

        first = eligible[0]
        footer = build_footer(settings)
        media = [message.media for message in eligible if has_media(message)]
        if media:
            files: Any = media if len(media) > 1 else media[0]
            sent = await client.send_file(
                settings.target_channel,
                files,
                caption=truncate_caption(merge_text(getattr(first, "raw_text", None), footer)),
                formatting_entities=getattr(first, "entities", None),
            )
        else:
            sent = await client.send_message(
                settings.target_channel,
                merge_text(getattr(first, "raw_text", None), footer),
                formatting_entities=getattr(first, "entities", None),
            )
        if sent is None:
            raise PrimarySendError(f"source send returned no result for id={ids}")
        source_state["last_source_message_id"] = checkpoint
        save_state(paths.source, source_state)
        result.source_sent += 1
        await _notify_best_effort(
            client,
            settings,
            settings.notify_group_chats,
            f"Опубликовано в {settings.target_channel}: source message_id={ids}",
            result,
            output,
        )

    if rss_state is None:
        return result

    items = await rss_items_loader(settings.rss_feed_url)
    seen = set(rss_state["recent_ids"])
    pending = _select_unseen_rss(items, seen, options.max_rss_items)
    for item in pending:
        result.rss_selected += 1
        text = await rss_post_builder(item)
        output(f"rss id={item['id']} destination={settings.rss_target_channel}")
        if options.shadow:
            continue
        sent = await client.send_message(settings.rss_target_channel, text)
        if sent is None:
            raise PrimarySendError(f"RSS send returned no result for id={item['id']}")
        updated_ids = item_keys(item) + [
            value for value in rss_state["recent_ids"] if value not in item_keys(item)
        ]
        rss_state["last_seen_id"] = item["id"]
        rss_state["recent_ids"] = updated_ids[: settings.rss_recent_limit]
        save_state(paths.rss, rss_state)
        result.rss_sent += 1
        await _notify_best_effort(
            client,
            settings,
            settings.notify_rss_chats,
            f"Опубликовано в {settings.rss_target_channel}: RSS {item['title']}",
            result,
            output,
        )
    return result


async def run_hourly(
    settings: Settings,
    client: Any,
    options: OnceOptions,
    paths: StatePaths,
    rss_items_loader: Any = parse_rss_metadata_items,
    rss_post_builder: Any = _default_rss_post_builder,
    output: Any = print,
    state_writer: Any = save_state,
) -> OnceResult:
    if options.seed_current and options.shadow:
        raise ValueError("seed and shadow modes cannot be combined")
    started_at = utc_now()
    result = OnceResult()
    connected = False
    with single_instance(paths.lock, write_pid=not options.shadow, allow_missing=options.shadow):
        if not options.shadow:
            save_state(paths.health, _health_payload("running", result, started_at))
        try:
            async def execute() -> OnceResult:
                nonlocal connected
                if options.seed_current:
                    seeded = await _seed_current(
                        settings, client, options, paths, rss_items_loader, state_writer
                    )
                    connected = True
                    return seeded
                caught_up = await _run_catch_up(
                    settings,
                    client,
                    options,
                    paths,
                    rss_items_loader,
                    rss_post_builder,
                    output,
                    result,
                )
                connected = True
                return caught_up

            result = await asyncio.wait_for(execute(), timeout=options.deadline_seconds)
            if not options.shadow:
                status = "seeded" if options.seed_current else "succeeded"
                save_state(
                    paths.health,
                    _health_payload(status, result, started_at, finished=True),
                )
            return result
        except Exception as exc:
            if not options.shadow:
                error = "deadline exceeded" if isinstance(exc, asyncio.TimeoutError) else str(exc)
                save_state(
                    paths.health,
                    _health_payload("failed", result, started_at, error=error, finished=True),
                )
            if isinstance(exc, (DurableStateError, PrimarySendError, asyncio.TimeoutError)):
                raise
            raise PrimarySendError(str(exc)) from exc
        finally:
            should_disconnect = connected or getattr(client, "started", False)
            is_connected = getattr(client, "is_connected", None)
            if callable(is_connected):
                with contextlib.suppress(Exception):
                    should_disconnect = should_disconnect or bool(is_connected())
            if should_disconnect:
                with contextlib.suppress(Exception):
                    await client.disconnect()


def _telegram_client(settings: Settings) -> TelegramClient:
    return TelegramClient(
        settings.session_name,
        settings.api_id,
        settings.api_hash,
        device_model="Redmi Note 8T",
        system_version="Android 11.0",
        app_version="10.14.5",
        lang_code="uk",
    )


async def daemon_main() -> None:
    settings = load_settings()
    require_nonempty(settings.api_hash, "API_HASH")
    require_nonempty(settings.source_chat, "SOURCE_CHAT")
    require_nonempty(settings.target_channel, "TARGET_CHANNEL")
    paths = StatePaths.from_environment()
    settings = replace(settings, rss_state_path=paths.rss)

    with single_instance(paths.lock):
        source_state = load_or_migrate_source_state(settings, paths)
        client = TelegramClient(
            settings.session_name, settings.api_id, settings.api_hash,
            device_model="Redmi Note 8T",
            system_version="Android 11.0",
            app_version="10.14.5",
            lang_code="uk"
        )
        pending_albums: dict[int, list[Any]] = defaultdict(list)
        source_chat_id = normalize_chat_id(settings.source_chat)
        health: dict[str, Any] = {
            "status": "starting",
            "pid": os.getpid(),
            "started_at": utc_now(),
            "last_heartbeat": utc_now(),
            "source_chat": settings.source_chat,
            "target_channel": settings.target_channel,
            "source_author_id": settings.source_author_id or "any",
            "rss_target_channel": settings.rss_target_channel or "off",
            "last_source_message_id": source_state["last_source_message_id"],
            "last_error": "",
        }
        save_state(paths.health, health)

        def update_health(**fields: Any) -> None:
            health.update(fields)
            health["pid"] = os.getpid()
            health["last_heartbeat"] = utc_now()
            save_state(paths.health, health)

        def checkpoint_source(message_id: int) -> None:
            save_state(paths.source, _source_payload(settings, int(message_id)))

        async def heartbeat_loop() -> None:
            while True:
                update_health(status="running")
                await asyncio.sleep(60)

        async def notify_many(chats: tuple[str, ...], text: str) -> None:
            recipients = [chat for chat in chats if chat]
            if not recipients and settings.notify_chat:
                recipients = [settings.notify_chat]
            if not recipients:
                return
            for chat in recipients:
                await send_with_retry(
                    f"notify:{chat}",
                    client.send_message,
                    chat,
                    text,
                )

        async def send_with_retry(label: str, sender: Any, *args: Any, **kwargs: Any) -> Any:
            delay = 2.0
            for attempt in range(1, 4):
                try:
                    result = await sender(*args, **kwargs)
                    update_health(last_error="")
                    return result
                except FloodWaitError as exc:
                    wait_for = max(1, min(getattr(exc, "seconds", 1), 300))
                    print(f"{label} flood-wait={wait_for}s", flush=True)
                    update_health(last_error=f"{label} flood-wait={wait_for}s")
                    await asyncio.sleep(wait_for)
                except ChatWriteForbiddenError as exc:
                    # No write access to target chat (or temporarily restricted).
                    update_health(last_error=f"{label} forbidden={exc}")
                    print(f"{label} forbidden={exc}", flush=True)
                    return None
                except Exception as exc:
                    err_str = str(exc)
                    if "caption is too long" in err_str.lower():
                        print(f"{label} fatal error (caption too long), skipping message", flush=True)
                        return None
                    
                    update_health(last_error=f"{label} error={exc}")
                    if attempt == 3:
                        raise
                    print(f"{label} retry={attempt} error={exc}", flush=True)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 15)

        async def rss_poll_loop(once: bool = False) -> None:
            if not settings.rss_feed_url or not settings.rss_target_channel:
                return

            state = load_state(settings.rss_state_path)
            recent_ids = [str(value).strip() for value in state.get("recent_ids", []) if str(value).strip()]
            last_seen_id = str(state.get("last_seen_id", "")).strip()

            while True:
                try:
                    items = await parse_rss_items(settings.rss_feed_url)
                    update_health(last_rss_check_at=utc_now())
                    if items:
                        newest_id = items[0]["id"]
                        if not last_seen_id:
                            last_seen_id = newest_id
                            seed_ids: list[str] = []
                            for item in items[: settings.rss_recent_limit]:
                                seed_ids.extend(item_keys(item))
                            recent_ids = seed_ids[: settings.rss_recent_limit]
                            save_state(
                                settings.rss_state_path, _rss_payload(settings, last_seen_id, recent_ids)
                            )
                            print(f"rss initialized last_seen_id={last_seen_id}", flush=True)
                            update_health(last_rss_seen_id=last_seen_id)
                        elif not recent_ids:
                            seeded_ids: list[str] = []
                            for item in items[: settings.rss_recent_limit]:
                                seeded_ids.extend(item_keys(item))
                            recent_ids = seeded_ids[: settings.rss_recent_limit]
                            save_state(
                                settings.rss_state_path, _rss_payload(settings, last_seen_id, recent_ids)
                            )
                            print(f"rss seeded recent_ids count={len(recent_ids)}", flush=True)
                            update_health(last_rss_seen_id=last_seen_id)
                        else:
                            pending = _select_unseen_rss(items, set(recent_ids), len(items))
                            for item in pending:
                                def checkpoint_rss(item: dict[str, str] = item) -> None:
                                    nonlocal last_seen_id, recent_ids
                                    keys = item_keys(item)
                                    recent_ids = (
                                        keys + [value for value in recent_ids if value not in keys]
                                    )[: settings.rss_recent_limit]
                                    last_seen_id = item["id"]
                                    save_state(
                                        settings.rss_state_path,
                                        _rss_payload(settings, last_seen_id, recent_ids),
                                    )
                                    update_health(
                                        last_rss_publish_at=utc_now(),
                                        last_rss_publish_id=item["id"],
                                        last_rss_publish_title=item["title"],
                                        last_rss_seen_id=last_seen_id,
                                    )
                                await publish_primary(
                                    lambda item=item: send_with_retry(
                                        f"rss:{settings.rss_target_channel}",
                                        client.send_message,
                                        settings.rss_target_channel,
                                        format_rss_post(item),
                                    ),
                                    checkpoint=checkpoint_rss,
                                    notify=lambda item=item: notify_many(
                                        settings.notify_rss_chats,
                                        f"Опубликовано в {settings.rss_target_channel}: RSS {item['title']}",
                                    ),
                                    label=f"RSS {item['id']}",
                                )
                                print(f"rss published id={item['id']} title={item['title']}", flush=True)
                                update_health(
                                    last_rss_publish_at=utc_now(),
                                    last_rss_publish_id=item["id"],
                                    last_rss_publish_title=item["title"],
                                )
                            last_seen_id = newest_id
                            updated_ids: list[str] = []
                            for item in items[: settings.rss_recent_limit]:
                                updated_ids.extend(item_keys(item))
                            recent_ids = updated_ids[: settings.rss_recent_limit]
                            save_state(
                                settings.rss_state_path, _rss_payload(settings, last_seen_id, recent_ids)
                            )
                            update_health(last_rss_seen_id=last_seen_id)
                    
                    if once:
                        break
                    await asyncio.sleep(settings.rss_poll_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"rss error: {exc}", flush=True)
                    update_health(last_error=f"rss error: {exc}")
                    if once:
                        break
                    await asyncio.sleep(min(settings.rss_poll_seconds, 60))

        async def publish_message(message: Any, commit: bool = True) -> None:
            processed_ids = load_processed_ids()
            if message.id in processed_ids:
                return # Уже отправляли
                
            footer = build_footer(settings)
            text = getattr(message, "raw_text", None)
            entities = getattr(message, "entities", None)

            def checkpoint(kind: str) -> None:
                save_processed_id(message.id, commit=commit)
                checkpoint_source(message.id)
                update_health(
                    last_group_publish_at=utc_now(),
                    last_group_publish_id=message.id,
                    last_group_publish_kind=kind,
                )

            if text and not getattr(message, "media", None):
                await publish_primary(
                    lambda: send_with_retry(
                        f"group:{settings.target_channel}:text",
                        client.send_message,
                        settings.target_channel,
                        merge_text(text, footer),
                        formatting_entities=entities,
                    ),
                    checkpoint=lambda: checkpoint("text"),
                    notify=lambda: notify_many(
                        settings.notify_group_chats,
                        f"Опубликовано в {settings.target_channel}: text, source message_id={message.id}",
                    ),
                    label=f"source message {message.id}",
                )
                print(f"published message_id={message.id} text", flush=True)
                return

            if has_media(message):
                kind = media_kind(message) or "unknown"
                await publish_primary(
                    lambda: send_with_retry(
                        f"group:{settings.target_channel}:media",
                        client.send_file,
                        settings.target_channel,
                        message.media,
                        caption=truncate_caption(merge_text(text, footer)),
                        formatting_entities=entities,
                    ),
                    checkpoint=lambda: checkpoint(kind),
                    notify=lambda: notify_many(
                        settings.notify_group_chats,
                        f"Опубликовано в {settings.target_channel}: media={kind}, source message_id={message.id}",
                    ),
                    label=f"source message {message.id}",
                )
                print(f"published message_id={message.id} media={kind}", flush=True)
                return

        async def catch_up() -> None:
            last_id = _source_state(settings, paths.source)["last_source_message_id"]

            print(f"catch-up: checking messages after id={last_id}", flush=True)
            try:
                entity = await client.get_entity(source_chat_id or settings.source_chat)
                count = 0
                async for msg in client.iter_messages(entity, min_id=int(last_id), reverse=True):
                    if should_skip_message(settings, msg):
                        checkpoint_source(msg.id)
                        continue
                    print(f"catch-up: processing missed message_id={msg.id}", flush=True)
                    await publish_message(msg, commit=False)
                    update_health(last_source_message_at=utc_now(), last_source_message_id=msg.id)
                    count += 1
                    if count % 10 == 0:
                        commit_processed_ids()
                    await asyncio.sleep(1)
                
                if count > 0:
                    commit_processed_ids()
            except Exception as exc:
                print(f"catch-up error: {exc}", flush=True)

        async def audit_loop() -> None:
            """Ленивая проверка пропусков раз в 30 минут"""
            while True:
                await asyncio.sleep(1800) # 30 минут
                print("audit: checking for missed messages...", flush=True)
                try:
                    entity = await client.get_entity(source_chat_id or settings.source_chat)
                    async for msg in client.iter_messages(entity, limit=20):
                        if not should_skip_message(settings, msg):
                            await publish_message(msg)
                except Exception as exc:
                    print(f"audit error: {exc}", flush=True)

        @client.on(events.NewMessage())
        async def on_new_message(event: events.NewMessage.Event) -> None:
            message = event.message
            if source_chat_id is not None:
                if event.chat_id != source_chat_id:
                    return
            else:
                if getattr(event.chat, "username", None) != settings.source_chat.lstrip("@"):
                    return
            if should_skip_message(settings, message):
                print(f"skipped message_id={message.id}", flush=True)
                checkpoint_source(message.id)
                return

            update_health(last_source_message_at=utc_now(), last_source_message_id=message.id)
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id:
                pending_albums[grouped_id].append(message)
                await asyncio.sleep(settings.album_wait_seconds)
                batch = pending_albums.get(grouped_id, [])
                if not batch or batch[-1].id != message.id:
                    return

                messages = pending_albums.pop(grouped_id, [])
                files = []
                for item in messages:
                    if not getattr(item, "media", None):
                        continue
                    files.append(item.media)
                if files:
                    footer = build_footer(settings)
                    caption = truncate_caption(merge_text(getattr(messages[0], "raw_text", None), footer))
                    await send_with_retry(
                        f"group:{settings.target_channel}:album",
                        client.send_file,
                        settings.target_channel,
                        files,
                        caption=caption,
                        formatting_entities=getattr(messages[0], "entities", None),
                    )
                    await notify_many(
                        settings.notify_group_chats,
                        f"Опубликован альбом в {settings.target_channel}: items={len(files)}, source grouped_id={grouped_id}"
                    )
                    print(f"published album grouped_id={grouped_id} items={len(files)}", flush=True)
                    update_health(
                        last_group_publish_at=utc_now(),
                        last_group_publish_id=grouped_id,
                        last_group_publish_kind=f"album:{len(files)}",
                    )
                    checkpoint_source(max(item.id for item in messages))
                return

            await publish_message(message)
            checkpoint_source(message.id)

        # ─── Freelance channel monitoring ───
        FREELANCE_CHANNELS = env_list("FREELANCE_CHANNELS")

        if FREELANCE_CHANNELS:
            @client.on(events.NewMessage(chats=FREELANCE_CHANNELS))
            async def on_freelance_message(event: events.NewMessage.Event) -> None:
                try:
                    text = event.message.message or ""
                    if not text:
                        return

                    chat = await event.get_chat()
                    chat_username = getattr(chat, 'username', str(chat.id))
                    post_link = f"https://t.me/{chat_username}/{event.message.id}"

                    payload = {
                        "title": shorten(text, 50).replace('\n', ' '),
                        "description": text,
                        "url": post_link,
                        "id": str(event.message.id),
                        "source": f"Telegram: @{chat_username}"
                    }

                    async with aiohttp.ClientSession() as session:
                        await session.post("http://localhost:8083/telegram_job", json=payload)
                except Exception as e:
                    print(f"Error processing freelance post: {e}", flush=True)

        run_once = "--once" in sys.argv
        await client.start()
        await catch_up()  # Досылаем то, что пропустили пока спали
        
        if run_once:
            print("Run once mode: performing final RSS poll and exiting.", flush=True)
            if settings.rss_feed_url and settings.rss_target_channel:
                await rss_poll_loop(once=True)
            update_health(status="stopped")
            await client.disconnect()
            return

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        asyncio.create_task(audit_loop()) # Запуск ленивой проверки
        rss_task = None
        if settings.rss_feed_url and settings.rss_target_channel:
            rss_task = asyncio.create_task(rss_poll_loop())
        print(
            "bridge started",
            f"source={settings.source_chat}",
            f"target={settings.target_channel}",
            f"author={settings.source_author_id or 'any'}",
            f"rss={settings.rss_target_channel or 'off'}",
            flush=True,
        )
        update_health(status="running", last_error="")
        try:
            await client.run_until_disconnected()
        finally:
            update_health(status="stopping")
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if rss_task is not None:
                rss_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rss_task
            update_health(status="stopped")


async def cli_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not (args.once or args.seed_current or args.shadow):
        await daemon_main()
        return 0

    settings = load_settings()
    require_nonempty(settings.api_hash, "API_HASH")
    require_nonempty(settings.source_chat, "SOURCE_CHAT")
    require_nonempty(settings.target_channel, "TARGET_CHANNEL")
    paths = StatePaths.from_environment()
    settings = replace(settings, rss_state_path=paths.rss)
    options = OnceOptions(
        seed_current=args.seed_current,
        force_reseed=args.force_reseed,
        shadow=args.shadow,
        max_source_items=args.max_source_items,
        max_rss_items=args.max_rss_items,
        deadline_seconds=args.deadline_seconds,
    )
    try:
        result = await run_hourly(settings, _telegram_client(settings), options, paths)
    except Exception as exc:
        print(f"bridge oneshot failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(
        "bridge oneshot complete",
        f"source_sent={result.source_sent}",
        f"rss_sent={result.rss_sent}",
        f"notifications_failed={result.notification_failures}",
        flush=True,
    )
    return 0


async def main() -> None:
    raise SystemExit(await cli_main())


if __name__ == "__main__":
    asyncio.run(main())
