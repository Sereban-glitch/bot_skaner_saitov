#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from telethon import TelegramClient


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str) -> int:
    value = env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return int(value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Telegram message via MTProto session")
    parser.add_argument("--to", required=True, help="Username, phone, or numeric peer id")
    parser.add_argument("--message", help="Message text")
    parser.add_argument("--file", help="Read message text from file")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    session_name = env("SESSION_NAME", ".mtproto-session")
    session_path = Path(session_name)
    if not session_path.is_absolute():
        session_path = base_dir / session_path

    message = args.message or ""
    if args.file:
        message = Path(args.file).read_text(encoding="utf-8")
    if not message:
        raise SystemExit("Provide --message or --file")

    client = TelegramClient(str(session_path), env_int("API_ID"), env("API_HASH"))
    await client.start()
    try:
        await client.send_message(args.to, message)
        print(f"sent to {args.to}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
