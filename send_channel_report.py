#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import re
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telethon import TelegramClient

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT_CHAT = '@sereban_tech'
HISTORY_FILE = BASE_DIR / 'report_stats.json'
DANGER_MAP_FILE = BASE_DIR / 'danger_map.json'

PLACE_KEYWORDS = [
    'запорожье', 'хортиц', 'коммунар', 'шевченков', 'александровск', 'правый', 'левый',
    'базар', 'рынок', 'анголенко', 'бабурка', 'пески', 'осипок', 'кичкас', 'кольцо', 'площадь',
    'проспект', 'мост', 'атб', 'варус', 'сильпо', 'вокзал', 'остановка'
]

EVENT_PATTERNS = {
    'тцк/военные': ['тцк', 'олива', 'зеленые', 'пиксель', 'военные', 'повестк', 'вручают', 'выписывают'],
    'полиция/копы': ['копы', 'синие', 'полиция', 'мусора', 'гвардия', 'патруль', 'проверка'],
    'транспорт': ['бус', 'микроавтобус', 'газель', 'пикап', 'джип'],
    'блокировка': ['блокпост', 'стоп', 'окружили', 'вяжут', 'прессуют', 'документ'],
}

# Patterns that signal "document check" risk (TSCH/military/police stops).
# When a post mentions a place + one of these → it's a risk point.
RISK_EVENT_PATTERNS = [
    'тцк', 'военком', 'военные', 'повестк', 'вручают', 'выписывают', 'раздают',
    'проверка', 'проверяют', 'документ', 'паспорт', 'призыв',
    'копы', 'синие', 'полиция', 'патруль', 'наряд',
    'блокпост', 'стоп', 'окружили', 'вяжут', 'прессуют', 'задерж',
    'военкомат', 'рейд', 'облава', 'кирпич', 'жёлтый',
]

# Additional place keywords specifically for risk points (districts, intersections).
RISK_PLACE_KEYWORDS = PLACE_KEYWORDS + [
    'плотина', 'дамба', 'шлюз', 'парк', 'кладбище', 'церковь', 'монастырь',
    'автовокзал', 'ж/д', 'вокзал', 'станция', 'метро', 'трамвай',
    'перекресток', 'развязка', 'поворот', 'светофор',
    'запорожский', 'днепровский', 'хортицкий', 'шелковский',
    'таврический', 'вознесеновский', 'александровский', 'бабуркинский',
    'кольцо', 'круг', 'площадь', 'майдан', 'улица',
]

STOPWORDS = {
    'это', 'как', 'все', 'так', 'был', 'что', 'для', 'под', 'над', 'там', 'тут', 'если', 'уже', 'есть'
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


def load_history() -> dict[str, int]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_history(date_str: str, score: int):
    history = load_history()
    history[date_str] = score
    sorted_dates = sorted(history.keys(), reverse=True)[:30]
    HISTORY_FILE.write_text(json.dumps({k: history[k] for k in sorted_dates}, indent=2), encoding='utf-8')


def update_danger_map(current_places: Counter):
    data = {}
    if DANGER_MAP_FILE.exists():
        try:
            data = json.loads(DANGER_MAP_FILE.read_text(encoding='utf-8'))
        except Exception:
            data = {}

    today_str = datetime.now().strftime('%Y-%m-%d')
    for place, count in current_places.items():
        if place not in data:
            data[place] = {}
        data[place][today_str] = data[place].get(today_str, 0) + count

    limit_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    cleaned_data = {}
    for place, history in data.items():
        recent_history = {d: c for d, c in history.items() if d >= limit_date}
        if recent_history:
            cleaned_data[place] = recent_history

    DANGER_MAP_FILE.write_text(json.dumps(cleaned_data, indent=2), encoding='utf-8')
    return cleaned_data


def get_hotspots(map_data: dict):
    totals = Counter()
    for place, history in map_data.items():
        totals[place] = sum(history.values())
    return totals.most_common(5)


def detect_risk_points(posts: list) -> list:
    """Detect places mentioned in posts that ALSO mention risk events
    (TSCH, military, police, document checks, raids).

    Returns list of tuples: [(place, count, sample_text), ...]
    Sorted by count descending, top 7.
    """
    risk_place_counts = Counter()
    risk_place_samples = {}

    for post in posts:
        text = post.text.lower()
        # Check if post mentions ANY risk event
        has_risk = any(p in text for p in RISK_EVENT_PATTERNS)
        if not has_risk:
            continue

        # Find which risk place is mentioned
        for place in RISK_PLACE_KEYWORDS:
            if place in text:
                risk_place_counts[place] += 1
                # Save first 100 chars of post as sample (for context)
                if place not in risk_place_samples:
                    sample = post.text.strip().replace('\n', ' ')[:120]
                    risk_place_samples[place] = sample

    # Return top 7 with samples
    result = []
    for place, count in risk_place_counts.most_common(7):
        result.append((place, count, risk_place_samples.get(place, '')))
    return result


def get_trend(current_score: int) -> str:
    history = load_history()
    if not history:
        return "Первая запись статистики."
    vals = list(history.values())
    avg = sum(vals) / len(vals)
    diff = current_score - avg
    emoji = "⚠️" if diff > 5 else "✅" if diff < -5 else "ℹ️"
    return f"{emoji} Тренд: {'+' if diff >= 0 else ''}{diff:.1f} к среднему (avg: {avg:.1f})"


def local_day_start():
    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc), now_local.astimezone(timezone.utc), now_local


@dataclass
class PostStats:
    id: int
    date: datetime
    views: int
    text: str


def situation_level(event_counts: Counter) -> tuple[str, int]:
    score = event_counts['тцк/военные'] * 3 + event_counts['полиция/копы'] * 2 + event_counts['блокировка'] * 4
    if score >= 30:
        return 'Критическая (рейды)', score
    if score >= 15:
        return 'Высокая активность', score
    if score >= 7:
        return 'Средняя (проверки)', score
    return 'Спокойная', score


async def main():
    load_dotenv(BASE_DIR / '.env')
    session_path = str(BASE_DIR / "analytics.session")
    client = TelegramClient(session_path, int(env('API_ID')), env('API_HASH'))
    await client.start()

    channel_ref = env('CHANNEL_REPORT_TARGET')
    report_chat = env('CHANNEL_REPORT_CHAT', DEFAULT_REPORT_CHAT)
    start_utc, end_utc, local_now = local_day_start()

    try:
        entity = await client.get_entity(channel_ref)
        posts = []
        words_counter = Counter()
        event_counts = Counter()
        place_counts = Counter()

        async for msg in client.iter_messages(entity, offset_date=end_utc):
            if msg.date < start_utc:
                break
            text = (msg.message or '').lower()
            posts.append(PostStats(msg.id, msg.date, getattr(msg, 'views', 0) or 0, text))

            for label, patterns in EVENT_PATTERNS.items():
                if any(p in text for p in patterns):
                    event_counts[label] += 1
            for p in PLACE_KEYWORDS:
                if p in text:
                    place_counts[p] += 1

            all_known = set(sum(EVENT_PATTERNS.values(), []) + PLACE_KEYWORDS + list(STOPWORDS))
            words = re.findall(r'[а-яё]{5,}', text)
            for w in words:
                if w not in all_known:
                    words_counter[w] += 1

        sit_text, score = situation_level(event_counts)
        trend = get_trend(score)
        save_history(local_now.strftime('%Y-%m-%d'), score)

        map_data = update_danger_map(place_counts)
        hotspots = get_hotspots(map_data)
        risk_points = detect_risk_points(posts)

        lines = [
            f"📊 Аналитика: {env('CHANNEL_REPORT_TITLE', channel_ref)}",
            f"📅 Дата: {local_now:%Y-%m-%d}",
            f"🔴 Обстановка: {sit_text}",
            f"📈 {trend}",
            "",
            f"📝 Сообщений: {len(posts)}",
            f"👁 Сумма просмотров: {sum(p.views for p in posts)}",
        ]

        if event_counts:
            lines.append("\n🔍 Сигналы:")
            for k, v in event_counts.most_common():
                lines.append(f"- {k}: {v}")

        if hotspots:
            lines.append("\n⚠️ ОЧАГИ ОПАСНОСТИ (за 7 дней):")
            for place, count in hotspots:
                lines.append(f"- {place.upper()}: {count} инцидентов")

        if risk_points:
            lines.append("\n📍 ТОЧКИ РИСКА (проверяют документы):")
            for place, count, sample in risk_points:
                lines.append(f"• {place.upper()} — {count} упоминаний")
                if sample:
                    lines.append(f'  └ "{sample}"')
            lines.append("\n🚨 Избегайте этих мест без документов (военный билет / приписное / справка).")

        new_words = words_counter.most_common(8)
        if new_words:
            lines.append("\n🧠 Новый сленг/темы дня: " + ", ".join(f"{k}" for k, v in new_words))

        await client.send_message(report_chat, "\n".join(lines))
        print(f"Report sent to {report_chat}. Hotspots: {len(hotspots)}")
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
