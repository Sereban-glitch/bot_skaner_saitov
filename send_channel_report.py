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
RISK_MAP_FILE = BASE_DIR / 'risk_map.json'  # 7-day history of risk-context mentions

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

# Kyiv timezone (UTC+3, EEST during summer). Server is UTC, convert for local analysis.
KYIV_TZ = timezone(timedelta(hours=3))

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


def update_risk_map(current_risk_places: Counter):
    """Update 7-day risk history: place -> {date: count}.
    Same shape as update_danger_map, but only for risk-context mentions.
    """
    data = {}
    if RISK_MAP_FILE.exists():
        try:
            data = json.loads(RISK_MAP_FILE.read_text(encoding='utf-8'))
        except Exception:
            data = {}

    today_str = datetime.now().strftime('%Y-%m-%d')
    for place, count in current_risk_places.items():
        if place not in data:
            data[place] = {}
        data[place][today_str] = data[place].get(today_str, 0) + count

    # Keep only last 7 days
    limit_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    cleaned = {}
    for place, history in data.items():
        recent = {d: c for d, c in history.items() if d >= limit_date}
        if recent:
            cleaned[place] = recent

    RISK_MAP_FILE.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False))
    return cleaned


def get_risk_patterns(risk_map: dict, min_days: int = 3):
    """Find places that appear on >=min_days in last 7 days.
    Returns list of tuples: [(place, days_count, total_mentions, last_date, avg_per_day), ...]
    Sorted by days_count DESC, then total_mentions DESC.
    """
    patterns = []
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    for place, history in risk_map.items():
        days_count = len(history)
        total = sum(history.values())
        if days_count < min_days:
            continue
        # Last seen date
        last_date = max(history.keys())
        # Is it active today or yesterday?
        is_active_recently = last_date >= yesterday
        # Average per active day
        avg = total / days_count if days_count > 0 else 0
        patterns.append((place, days_count, total, last_date, avg, is_active_recently))

    # Sort: by days_count DESC, then total DESC
    patterns.sort(key=lambda x: (-x[1], -x[2]))
    return patterns


def analyze_time_patterns(posts: list, days: int = 7) -> dict:
    """Analyze when risk events happen during the day (Kyiv timezone).

    Returns dict with:
      - risk_hour_counts: {hour: count} for posts with risk events
      - all_hour_counts: {hour: count} for all posts
      - peak_hours: top 3 hours with most risk events
      - quiet_hours: hours with lowest risk events (but >0)
      - total_risk_posts: int
      - total_posts: int
      - days_analyzed: int
    """
    risk_hour_counts = Counter()
    all_hour_counts = Counter()
    risk_event_keywords = sum([RISK_EVENT_PATTERNS], [])  # flatten

    for post in posts:
        # Convert UTC to Kyiv time
        kyiv_time = post.date.astimezone(KYIV_TZ)
        hour = kyiv_time.hour
        all_hour_counts[hour] += 1

        text = post.text.lower()
        if any(p in text for p in RISK_EVENT_PATTERNS):
            risk_hour_counts[hour] += 1

    # Find peak hours (top 3 by risk count)
    peak = sorted(risk_hour_counts.items(), key=lambda x: -x[1])[:3]
    # Find quiet hours (lowest with >0 risk events)
    nonzero = [(h, c) for h, c in risk_hour_counts.items() if c > 0]
    quiet = sorted(nonzero, key=lambda x: x[1])[:3]

    return {
        'risk_hour_counts': dict(risk_hour_counts),
        'all_hour_counts': dict(all_hour_counts),
        'peak_hours': peak,
        'quiet_hours': quiet,
        'total_risk_posts': sum(risk_hour_counts.values()),
        'total_posts': len(posts),
        'days_analyzed': days,
    }


def format_hour_bar(count: int, max_count: int, width: int = 20) -> str:
    """Format a horizontal bar chart for hour distribution."""
    if max_count == 0:
        return ''
    filled = int((count / max_count) * width)
    return '█' * filled + '░' * (width - filled)


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

    # For time pattern analysis: fetch last 7 days
    week_start_utc = end_utc - timedelta(days=7)

    try:
        entity = await client.get_entity(channel_ref)
        posts = []  # today's posts (for main stats)
        week_posts = []  # last 7 days (for time patterns)
        words_counter = Counter()
        event_counts = Counter()
        place_counts = Counter()

        async for msg in client.iter_messages(entity, offset_date=end_utc):
            if msg.date < week_start_utc:
                break
            text = (msg.message or '').lower()
            post_obj = PostStats(msg.id, msg.date, getattr(msg, 'views', 0) or 0, text)
            week_posts.append(post_obj)

            # Only today's posts go into main stats
            if msg.date >= start_utc:
                posts.append(post_obj)
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
        # Accumulate risk place counts for 7-day pattern analysis
        risk_place_counts = Counter()
        for place, count, _ in risk_points:
            risk_place_counts[place] = count
        risk_map = update_risk_map(risk_place_counts)
        risk_patterns = get_risk_patterns(risk_map, min_days=3)
        time_patterns = analyze_time_patterns(week_posts, days=7)

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

        if risk_patterns:
            lines.append("\n🔥 ПАТТЕРН РИСКА (повторяются 3+ дней за неделю):")
            for place, days, total, last_date, avg, active in risk_patterns:
                marker = "🔴" if active else "⚪"  # red if seen today/yesterday
                lines.append(
                    f"{marker} {place.upper()} — {days}/7 дн, {total} упом."
                    f", посл. {last_date}, ~{avg:.1f}/дн"
                )
            lines.append("\n💡 Это места где ТЦК/полиция появляются регулярно. Обходить стороной.")

        # Time patterns (based on 7 days of data)
        if time_patterns and time_patterns['total_risk_posts'] > 0:
            tp = time_patterns
            lines.append(f"\n⏰ ВРЕМЕННЫЕ ПАТТЕРНЫ (за {tp['days_analyzed']} дн, риск-события по Киеву):")
            lines.append(f"Всего риск-постов: {tp['total_risk_posts']} из {tp['total_posts']} ({100*tp['total_risk_posts']/max(tp['total_posts'],1):.0f}%)")

            # Peak hours
            if tp['peak_hours']:
                lines.append("\n🔴 ПИКОВЫЕ ЧАСЫ (опасно):")
                for hour, count in tp['peak_hours']:
                    bar = format_hour_bar(count, tp['peak_hours'][0][1])
                    lines.append(f"  {hour:02d}:00–{hour+1:02d}:00  {bar} {count}")

            # Quiet hours
            if tp['quiet_hours']:
                lines.append("\n🟢 СПОКОЙНЫЕ ЧАСЫ (меньше риска):")
                for hour, count in tp['quiet_hours']:
                    bar = format_hour_bar(count, tp['peak_hours'][0][1] if tp['peak_hours'] else 1)
                    lines.append(f"  {hour:02d}:00–{hour+1:02d}:00  {bar} {count}")

            # Daily hour histogram (compact, every 3 hours)
            lines.append("\n📊 Распределение по часам (Киев):")
            rh = tp['risk_hour_counts']
            max_count = max(rh.values()) if rh else 1
            for h in range(0, 24, 3):
                count = rh.get(h, 0) + rh.get(h+1, 0) + rh.get(h+2, 0)
                bar = format_hour_bar(count, max_count * 3, width=15)
                lines.append(f"  {h:02d}-{h+3:02d}  {bar} {count}")

            lines.append("\n💡 Планируйте поездки на спокойные часы. Избегайте пиковых.")

        new_words = words_counter.most_common(8)
        if new_words:
            lines.append("\n🧠 Новый сленг/темы дня: " + ", ".join(f"{k}" for k, v in new_words))

        await client.send_message(report_chat, "\n".join(lines))
        print(f"Report sent to {report_chat}. Hotspots: {len(hotspots)}")
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
