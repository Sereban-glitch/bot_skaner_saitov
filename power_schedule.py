#!/usr/bin/env python3
"""
Power Schedule Monitor for Zaporizhzhia (GPV)
Scans @Zaporizhzhyaoblenergo_news for power schedules,
posts structured summaries to @svobodnye_ludi_zp,
and sends personalized alerts for queue 1.2 to family members:
- Sergey (@sereban_tech)
- Ksyusha (@Ksuhavorobey)
- Miron (@depicTOMii)

Updates are delivered in-place: the channel post is edited, personal
messages are re-sent (so the recipient gets a push) and the previous
one is deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import FloodWaitError

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "power_schedule_state.json"
LOG_FILE = BASE_DIR / "power_schedule.log"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("power_schedule")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_schedule(text: str) -> dict:
    result: dict[str, Any] = {
        "is_schedule": False,
        "date_str": "",
        "is_update": False,
        "queues": {},
        "raw_text": text,
    }

    if not text:
        return result

    keywords = ["графік", "гпв", "відключен", "електропостачанн", "черг"]
    if not any(k in text.lower() for k in keywords):
        return result

    if any(u in text.lower() for u in ["оновлено", "змінено", "увага"]):
        result["is_update"] = True

    # Date match: Ukrainian month name with 1 or 2 digits
    date_match = re.search(
        r"\b(\d{1,2})\s+(січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня)",
        text,
        re.IGNORECASE,
    )
    if date_match:
        result["date_str"] = f"{date_match.group(1)} {date_match.group(2).lower()}"

    # Split by queue headers: 1.1 through 6.2
    sections = re.split(
        r"(?:⚡\s*)?(?:(?:Черга|черга)\s*)?(\b[1-6]\.[12]\b)\s*[:–—\-]?", text
    )
    if len(sections) > 1:
        for i in range(1, len(sections), 2):
            q_num = sections[i].strip()
            q_body = sections[i + 1]

            footer_match = re.search(
                r"(?:Також|Актуальні|ГОП|До уваги)", q_body, re.IGNORECASE
            )
            if footer_match:
                q_body = q_body[: footer_match.start()]

            intervals = re.findall(
                r"(?:з|c)?\s*(\d{1,2}:\d{2})\s*(?:до|[–—\-])\s*(\d{1,2}:\d{2})",
                q_body,
                re.IGNORECASE,
            )
            if intervals:
                seen = set()
                clean = []
                for start, end in intervals:
                    pair = f"{start} – {end}"
                    if pair not in seen:
                        seen.add(pair)
                        clean.append(pair)
                if clean:
                    result["queues"][q_num] = clean

    if result["queues"]:
        result["is_schedule"] = True

    return result


def format_channel_post(data: dict) -> str:
    date_str = data.get("date_str") or "сьогодні"
    is_update = data.get("is_update", False)
    source_time = data.get("source_time") or ""
    header = (
        "⚠️ ОНОВЛЕННЯ ГРАФІКА ВІДКЛЮЧЕНЬ СВІТЛА"
        if is_update
        else "⚡️ ГРАФІК ВІДКЛЮЧЕНЬ СВІТЛА У ЗАПОРІЖЖІ"
    )

    queues = data.get("queues", {})
    lines = [
        f"{header}",
        f"📅 Дата: {date_str}",
    ]
    if source_time:
        lines.append(f"🕒 За даними обленерго на {source_time}")

    lines.extend(["", "🔴 Години відсутності електроенергії по чергах:"])

    for main_q in range(1, 7):
        sub1 = f"{main_q}.1"
        sub2 = f"{main_q}.2"
        q_lines = []
        if sub1 in queues:
            q_lines.append(f"  • {sub1}: {' | '.join(queues[sub1])}")
        if sub2 in queues:
            q_lines.append(f"  • {sub2}: {' | '.join(queues[sub2])}")

        if q_lines:
            lines.append(f"\n🔸 Черга {main_q}:")
            lines.extend(q_lines)

    lines.append("\n⏱ Час у графіку вказано з урахуванням 30 хв на перемикання.")
    lines.append("🔗 Джерело: @Zaporizhzhyaoblenergo_news")
    lines.append("ℹ️ За офіційними даними «Запоріжжяобленерго».")
    lines.append("🍁 Вільні Люди Запоріжжя")
    return "\n".join(lines)


def format_personal_messages(data: dict) -> dict[str, str]:
    date_str = data.get("date_str") or "сегодня"
    is_update = data.get("is_update", False)
    source_time = data.get("source_time") or ""
    q_1_2 = data.get("queues", {}).get("1.2", [])

    if q_1_2:
        intervals_str = "\n".join([f"🔴 {inv}" for inv in q_1_2])
    else:
        intervals_str = "Отключения для очереди 1.2 не запланированы (свет есть)."

    status_prefix = "⚠️ Внимание, график изменился!\n\n" if is_update else ""
    if source_time:
        status_prefix += f"🕒 Данные облэнерго на {source_time}.\n\n"

    ksusha_variants = [
        f"{status_prefix}Ксюша, привет! 💡 Свежий график по нашей 1.2 очереди на {date_str}:\n\n{intervals_str}\n\nНе забудь поставить телефон и пауэрбанк на зарядку! ❤️",
        f"{status_prefix}Ксюша, добрый день! ✨ Расписание отключений света для 1.2 на {date_str}:\n\n{intervals_str}\n\nСпланируй дела заранее и заряди гаджеты! 🌸",
        f"{status_prefix}Ксюшенька, привет! ⚡️ Пришло расписание по свету (очередь 1.2) на {date_str}:\n\n{intervals_str}\n\nДержи заряженным телефон и береги себя! 🕯✨",
    ]

    miron_variants = [
        f"{status_prefix}Мирон, салют! ⚡️ Держи график света для нашей 1.2 очереди на {date_str}:\n\n{intervals_str}\n\nПроверь зарядку на телефоне и пауэрбанке, чтобы не остаться без связи! 🔋🎮",
        f"{status_prefix}Мирон, привет! 💡 Инфа по отключениям для очереди 1.2 на {date_str}:\n\n{intervals_str}\n\nПоставь всё вовремя на зарядку! ⚡️📱",
        f"{status_prefix}Мирон, здорово! 🔌 Свежее расписание по свету (1.2) на {date_str}:\n\n{intervals_str}\n\nЗаряди гаджеты заранее! ⚡️🕹",
    ]

    sergey_variants = [
        f"{status_prefix}Сергей, статус по нашей 1.2 очереди на {date_str}:\n\n{intervals_str}\n\nКанал оповещён, семье отправлено.",
        f"{status_prefix}Сергей, информация по свету (очередь 1.2) на {date_str}:\n\n{intervals_str}\n\nВсё разослано, статус в норме.",
        f"{status_prefix}Сергей, свежая сводка по 1.2 на {date_str}:\n\n{intervals_str}\n\nОповещения доставлены.",
    ]

    return {
        "sergey": random.choice(sergey_variants),
        "ksusha": random.choice(ksusha_variants),
        "miron": random.choice(miron_variants),
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Error loading state: {e}")
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving state: {e}")


def is_quiet_hours() -> bool:
    """Return True if current Kyiv time is between 23:00 and 07:00."""
    now = datetime.now(KYIV_TZ)
    return now.hour >= 23 or now.hour < 7


def source_stamp(msg) -> tuple[str, str]:
    """Return (iso_timestamp, HH:MM) of the source post in Kyiv time."""
    dt = (msg.edit_date or msg.date).astimezone(KYIV_TZ)
    return dt.isoformat(), dt.strftime("%H:%M")


def schedule_hash(data: dict, source_msg_id: Optional[int], stamp_iso: str) -> str:
    """Hash includes the date and the source post revision, so a new day
    or an edited/reposted source is always treated as a change."""
    payload = {
        "date": data.get("date_str", ""),
        "queues": data.get("queues", {}),
        "source_msg_id": source_msg_id,
        "source_stamp": stamp_iso,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def deliver_channel(
    client: TelegramClient, target_channel: str, text: str, state: dict, force_new: bool = False
) -> int:
    """Edit the previous channel post in place; send a new one if editing fails
    or if the schedule belongs to a new day (so subscribers get a notification)."""
    old_id = state.get("channel_message_id")
    if old_id and not force_new:
        try:
            await client.edit_message(target_channel, int(old_id), text)
            logger.info(f"Channel post {old_id} updated in place.")
            return int(old_id)
        except Exception as e:
            logger.warning(f"Could not edit channel message {old_id}: {e}. Sending a new post.")
    msg = await client.send_message(target_channel, text)
    logger.info(f"New channel post sent, id={msg.id}.")
    return int(msg.id)


async def deliver_pm(
    client: TelegramClient, username: str, text: str, label: str, state: dict, key: str
) -> int:
    """Send a new PM (so the recipient gets a push), then delete the old one."""
    try:
        logger.info(f"Sending PM to {label} ({username})...")
        msg = await client.send_message(username, text)
    except FloodWaitError as e:
        logger.warning(f"FloodWait encountered ({e.seconds}s), sleeping...")
        await asyncio.sleep(e.seconds + 1)
        msg = await client.send_message(username, text)
    logger.info(f"PM to {label} sent successfully, id={msg.id}.")

    pm_ids = state.setdefault("pm_message_ids", {})
    old_id = pm_ids.get(key)
    if old_id and int(old_id) != int(msg.id):
        try:
            await client.delete_messages(username, [int(old_id)])
            logger.info(f"Previous PM {old_id} to {label} deleted.")
        except Exception as e:
            logger.warning(f"Could not delete previous PM {old_id} to {label}: {e}")

    pm_ids[key] = int(msg.id)
    return int(msg.id)


async def run_sync(
    preview: bool = False,
    test_pm_only: bool = False,
    force: bool = False,
    ignore_quiet_hours: bool = False,
    source_channel: str = "@Zaporizhzhyaoblenergo_news",
    target_channel: str = "@svobodnye_ludi_zp",
) -> None:
    load_dotenv(BASE_DIR / ".env")
    api_id_val = os.getenv("API_ID")
    api_hash_val = os.getenv("API_HASH")

    if not api_id_val or not api_hash_val:
        logger.error("API_ID or API_HASH missing in .env")
        return

    session_file = str(BASE_DIR / "analytics.session")
    client = TelegramClient(session_file, int(api_id_val), api_hash_val)
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("Telegram client is NOT authorized in analytics.session")
        await client.disconnect()
        return

    try:
        logger.info(f"Checking source channel {source_channel}...")
        source_entity = await client.get_entity(source_channel)

        latest_schedule: Optional[dict] = None
        source_msg_id: Optional[int] = None
        stamp_iso = ""
        stamp_hhmm = ""

        async for msg in client.iter_messages(source_entity, limit=10):
            if not msg.text:
                continue
            parsed = parse_schedule(msg.text)
            if parsed["is_schedule"] and len(parsed["queues"]) >= 4:
                stamp_iso, stamp_hhmm = source_stamp(msg)
                parsed["source_time"] = stamp_hhmm
                latest_schedule = parsed
                source_msg_id = msg.id
                break

        if not latest_schedule:
            logger.info("No active power schedule found in recent messages.")
            return

        queues = latest_schedule["queues"]
        date_str = latest_schedule["date_str"]
        is_update = latest_schedule["is_update"]
        schedule_hash_value = schedule_hash(latest_schedule, source_msg_id, stamp_iso)

        logger.info(
            f"Found schedule: msg_id={source_msg_id}, date='{date_str}', "
            f"stamp={stamp_hhmm}, is_update={is_update}, queues={len(queues)}"
        )

        state = load_state()
        last_hash = state.get("last_schedule_hash")

        if not force and not preview and not test_pm_only and last_hash == schedule_hash_value:
            logger.info("Schedule has not changed (hash matches). Nothing to send.")
            return

        # Quiet hours protection (23:00 - 07:00 Kyiv)
        if not preview and not test_pm_only and not ignore_quiet_hours and is_quiet_hours():
            logger.info(
                f"Quiet hours active in Kyiv ({datetime.now(KYIV_TZ).strftime('%H:%M')}). Postponing delivery until 07:00."
            )
            return

        channel_post = format_channel_post(latest_schedule)
        personal_msgs = format_personal_messages(latest_schedule)

        if preview:
            print("\n" + "=" * 50)
            print("[PREVIEW MODE - NOTHING WILL BE SENT]")
            print("=" * 50)
            print("\n--- CHANNEL POST (@svobodnye_ludi_zp) ---")
            print(channel_post)
            print("\n--- SERGEY (@sereban_tech) ---")
            print(personal_msgs["sergey"])
            print("\n--- KSYUSHA (@Ksuhavorobey) ---")
            print(personal_msgs["ksusha"])
            print("\n--- MIRON (@depicTOMii) ---")
            print(personal_msgs["miron"])
            print("=" * 50 + "\n")
            return

        if test_pm_only:
            logger.info("Sending TEST PM to Sergey (@sereban_tech)...")
            test_text = (
                "🧪 ТЕСТОВЫЙ ПРОГОН МОДУЛЯ ГРАФИКОВ СВЕТА ⚡️\n\n"
                + personal_msgs["sergey"]
                + "\n\n(Это тестовое сообщение, отправлено только тебе)."
            )
            await client.send_message("@sereban_tech", test_text)
            logger.info("Test PM sent successfully to @sereban_tech!")
            return

        # 1. Channel post: edit the previous one, do not multiply messages.
        # A new day gets a fresh post so subscribers receive a notification.
        same_day = state.get("channel_message_date") == date_str
        logger.info(f"Publishing schedule to channel {target_channel}...")
        try:
            state["channel_message_id"] = await deliver_channel(
                client, target_channel, channel_post, state, force_new=not same_day
            )
            state["channel_message_date"] = date_str
            save_state(state)
        except Exception as e:
            logger.error(f"Failed to publish to {target_channel}: {e}")

        await asyncio.sleep(3)

        # 2. Personal PMs: new message (push) + delete the previous one
        recipients = [
            ("@sereban_tech", personal_msgs["sergey"], "Sergey", "sergey"),
            ("@Ksuhavorobey", personal_msgs["ksusha"], "Ksyusha", "ksusha"),
            ("@depicTOMii", personal_msgs["miron"], "Miron", "miron"),
        ]

        for username, text, label, key in recipients:
            try:
                await deliver_pm(client, username, text, label, state, key)
                save_state(state)
            except Exception as e:
                logger.error(f"Failed to send PM to {label} ({username}): {e}")

            await asyncio.sleep(random.uniform(3.0, 5.0))

        # 3. Update State
        state["last_msg_id"] = source_msg_id
        state["last_schedule_hash"] = schedule_hash_value
        state["last_date"] = date_str
        state["last_source_time"] = stamp_hhmm
        state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        logger.info("Power schedule sync finished successfully!")

    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Power Schedule Monitor")
    parser.add_argument("--preview", action="store_true", help="Print formatted messages without sending")
    parser.add_argument("--test-pm", action="store_true", help="Send a test PM only to Sergey (@sereban_tech)")
    parser.add_argument("--force", action="store_true", help="Ignore last hash and force send")
    parser.add_argument("--ignore-quiet-hours", action="store_true", help="Send even during quiet hours (23:00-07:00)")
    parser.add_argument("--target-channel", default="@svobodnye_ludi_zp", help="Target channel")
    args = parser.parse_args()

    asyncio.run(
        run_sync(
            preview=args.preview,
            test_pm_only=args.test_pm,
            force=args.force,
            ignore_quiet_hours=args.ignore_quiet_hours,
            target_channel=args.target_channel,
        )
    )


if __name__ == "__main__":
    main()
