#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, User

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CHANNEL_ID = -1001191043502
DEFAULT_GROUP_ID = -1001190971170
DEFAULT_REPORT_CHAT = "@random_jssb"
STOPWORDS = {
    'и','в','на','с','по','к','у','за','от','до','из','что','как','но','а','или','не','это','то','мы','вы','он','она','они',
    'я','ты','о','об','для','под','над','же','ли','бы','уже','ещё','еще','так','там','тут','его','ее','её','их','наш','ваш',
    'из','без','при','про','ну','да','нет','если','только','вот','все','всё'
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name)
    return int(raw) if raw else default


@dataclass
class PostStats:
    id: int
    date: datetime
    views: int
    forwards: int
    reactions: int
    kind: str
    text: str


def local_day_start() -> tuple[datetime, datetime, datetime]:
    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc), now_local.astimezone(timezone.utc), now_local


def normalize_text(text: str) -> list[str]:
    words = re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄє0-9_'-]{4,}", text.lower())
    return [w for w in words if w not in STOPWORDS]


def post_kind(message) -> str:
    media = getattr(message, 'media', None)
    if media is None:
        return 'text'
    if isinstance(media, MessageMediaPhoto):
        return 'photo'
    if isinstance(media, MessageMediaDocument):
        return 'document/video'
    return 'media'


def short(text: str, limit: int = 90) -> str:
    text = re.sub(r'\s+', ' ', (text or '').strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


async def collect_channel(client: TelegramClient, channel_id: int, username: str | None, start_utc: datetime, end_utc: datetime):
    posts: list[PostStats] = []
    async for msg in client.iter_messages(channel_id, offset_date=end_utc):
        if msg.date < start_utc:
            break
        if msg.date > end_utc:
            continue
        reactions = 0
        result = getattr(getattr(msg, 'reactions', None), 'results', None)
        if result:
            reactions = sum(getattr(item, 'count', 0) for item in result)
        posts.append(PostStats(
            id=msg.id,
            date=msg.date,
            views=getattr(msg, 'views', 0) or 0,
            forwards=getattr(msg, 'forwards', 0) or 0,
            reactions=reactions,
            kind=post_kind(msg),
            text=(msg.message or ''),
        ))
    top = sorted(posts, key=lambda p: (p.views, p.forwards, p.reactions), reverse=True)[:3]
    links = []
    if username:
        for p in top:
            links.append(f"- {p.views} views | {short(p.text) or p.kind} | https://t.me/{username}/{p.id}")
    media_posts = sum(1 for p in posts if p.kind != 'text')
    return {
        'count': len(posts),
        'media_posts': media_posts,
        'total_views': sum(p.views for p in posts),
        'total_forwards': sum(p.forwards for p in posts),
        'total_reactions': sum(p.reactions for p in posts),
        'top_lines': links,
    }


async def collect_group(client: TelegramClient, group_id: int, start_utc: datetime, end_utc: datetime):
    total = 0
    senders = Counter()
    words = Counter()
    async for msg in client.iter_messages(group_id, offset_date=end_utc):
        if msg.date < start_utc:
            break
        if msg.date > end_utc:
            continue
        if not (msg.message or '').strip() and not getattr(msg, 'media', None):
            continue
        total += 1
        sender_id = getattr(msg, 'sender_id', None)
        if sender_id:
            senders[sender_id] += 1
        if msg.message:
            words.update(normalize_text(msg.message))
    top_users = []
    for sender_id, count in senders.most_common(5):
        try:
            entity = await client.get_entity(sender_id)
            if isinstance(entity, User):
                name = ' '.join(part for part in [entity.first_name, entity.last_name] if part).strip() or (entity.username or str(sender_id))
            else:
                name = getattr(entity, 'title', None) or str(sender_id)
        except Exception:
            name = str(sender_id)
        top_users.append(f"- {name}: {count}")
    top_words = [f"{word} ({count})" for word, count in words.most_common(8)]
    return {
        'count': total,
        'participants': len(senders),
        'top_users': top_users,
        'top_words': top_words,
    }


async def main() -> None:
    load_dotenv(BASE_DIR / '.env')
    session = env('SESSION_NAME', '.mtproto-session')
    session_path = Path(session)
    if not session_path.is_absolute():
        # Используем выделенную сессию для аналитики
        session_path = BASE_DIR / "analytics.session"
        api_id = int(env('API_ID'))
        api_hash = env('API_HASH')
        report_chat = env('SVOBOD_REPORT_CHAT', DEFAULT_REPORT_CHAT)
        channel_id = env_int('SVOBOD_CHANNEL_ID', DEFAULT_CHANNEL_ID)
        group_id = env_int('SVOBOD_GROUP_ID', DEFAULT_GROUP_ID)
        start_utc, end_utc, local_now = local_day_start()
        client = TelegramClient(str(session_path), api_id, api_hash)
        await client.start()
    try:
        channel_entity = await client.get_entity(channel_id)
        username = getattr(channel_entity, 'username', None)
        channel = await collect_channel(client, channel_id, username, start_utc, end_utc)
        group = await collect_group(client, group_id, start_utc, end_utc)
        lines = [
            f"Вечерняя аналитика СЛЗ за {local_now:%Y-%m-%d}",
            "",
            "Канал:",
            f"- Постов: {channel['count']}",
            f"- Медиа-постов: {channel['media_posts']}",
            f"- Сумма просмотров: {channel['total_views']}",
            f"- Сумма пересылок: {channel['total_forwards']}",
            f"- Сумма реакций: {channel['total_reactions']}",
            "",
            "Чат:",
            f"- Сообщений: {group['count']}",
            f"- Активных участников: {group['participants']}",
        ]
        if channel['top_lines']:
            lines.extend(["", "Топ постов канала:"] + channel['top_lines'])
        if group['top_users']:
            lines.extend(["", "Топ участников чата:"] + group['top_users'])
        if group['top_words']:
            lines.extend(["", "Частые темы:", "- " + ", ".join(group['top_words'])])
        await client.send_message(report_chat, '\n'.join(lines))
        print(f"sent report to {report_chat}")
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
