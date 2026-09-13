#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import xml.etree.ElementTree as ET
from typing import Any

from telethon import TelegramClient

import bridge


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


async def load_preview_rss_items(feed_url: str) -> list[dict[str, str]]:
    payload = await bridge.fetch_url_text(feed_url)
    if not payload:
        return []

    try:
        root = ET.fromstring(payload.encode("utf-8"))
    except ET.ParseError:
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items: list[dict[str, str]] = []
    for item in channel.findall("item")[:1]:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "excerpt": bridge.shorten(bridge.strip_html(item.findtext("description")), 650),
                "link": link,
            }
        )
    return items


def format_source_preview(settings: bridge.Settings, message: Any) -> str:
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or "[медиа без текста]"
    body = bridge.shorten(text, 3500)
    return (
        "[TEST] MTProto: сообщение Telegram\n"
        f"Источник: {settings.source_link_label}, message_id={message.id}\n\n"
        f"{body}"
    )


def format_rss_preview(item: dict[str, str]) -> str:
    return "[TEST] MTProto: RSS domdara.org\n\n" + bridge.shorten(bridge.format_rss_post(item), 3800)


async def run_preview(
    settings: bridge.Settings,
    preview_chat: str,
    *,
    client: Any | None = None,
    rss_items: list[dict[str, str]] | None = None,
    paths: bridge.StatePaths | None = None,
) -> None:
    if client is None:
        client = TelegramClient(
            settings.session_name,
            settings.api_id,
            settings.api_hash,
            device_model="Redmi Note 8T",
            system_version="Android 11.0",
            app_version="10.14.5",
            lang_code="uk",
        )

    # Use paths.lock if provided, otherwise default to BRIDGE_STATE_DIR or local directory
    lock_path = paths.lock if paths else os.path.join(os.environ.get("BRIDGE_STATE_DIR", BASE_DIR), "bridge.lock")

    with bridge.single_instance(lock_path):
        await client.start()
        try:
            entity = await client.get_entity(bridge.normalize_chat_id(settings.source_chat) or settings.source_chat)
            async for message in client.iter_messages(entity, limit=100):
                if bridge.should_skip_message(settings, message):
                    continue
                await client.send_message(preview_chat, format_source_preview(settings, message))
                break

            if rss_items is None:
                rss_items = await load_preview_rss_items(settings.rss_feed_url) if settings.rss_feed_url else []
            if rss_items:
                await client.send_message(preview_chat, format_rss_preview(rss_items[0]))
        finally:
            await client.disconnect()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send an isolated MTProto preview to a test chat")
    parser.add_argument("--chat", required=True, help="Test chat username or numeric ID")
    args = parser.parse_args()

    bridge.load_dotenv(os.path.join(BASE_DIR, ".env"))
    settings = bridge.load_settings()
    await run_preview(settings, args.chat)


if __name__ == "__main__":
    asyncio.run(main())
