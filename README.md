# Telegram MTProto Bridge

Minimal Telethon bridge that can republish posts from a Telegram group into a Telegram channel and mirror a site RSS feed into another Telegram channel using a user account.

## What it does

- Listens for new messages in one source group
- Republishes matching messages into one target channel as fresh channel posts
- Preserves the original text and caption content
- Buffers album messages briefly so grouped media can be sent together
- Optionally filters by author id
- Optionally polls one RSS feed and posts new entries into another channel

## Requirements

- Python 3.11+
- `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)
- A Telegram user session that can read the source group and post to the target channel

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in `API_ID`, `API_HASH`, `SOURCE_CHAT`, and `TARGET_CHANNEL`.
   Optionally add `RSS_FEED_URL` and `RSS_TARGET_CHANNEL` for site mirroring.
3. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

4. Run the bridge:

```bash
python3 bridge.py
```

Or use the helper scripts:

```bash
./start.sh
./restart.sh
./status.sh
./logs.sh
./healthcheck.sh
./stop.sh
```

Send a direct Telegram report without the bot:

```bash
python3 send_mtproto_message.py --to @username --message "Bridge is healthy"
```

## Notes

- The first launch will ask for your phone number, login code, and possibly a 2FA password.
- `SOURCE_AUTHOR_ID` is optional. Leave it empty to mirror all non-system messages from the source group.
- `NOTIFY_CHAT` is optional. Use a username like `@random_jssb` to receive service notifications after publishes.
- `NOTIFY_GROUP_CHATS` is optional. Comma-separated usernames/chats for `Radio Kovtun -> channel` notifications.
- `NOTIFY_RSS_CHATS` is optional. Comma-separated usernames/chats for `domdara RSS -> channel` notifications.
- `send_mtproto_message.py` uses the same MTProto session and sends messages directly from your Telegram account, without `@AdimnRobot`.
- `RSS_FEED_URL` should use `http` for `domdara.org`, because the site certificate is broken on `https`.
- `RSS_RECENT_LIMIT` controls how many recent RSS identifiers are kept for deduplication.
- The bridge keeps a lock file in `/root/telegram-mtproto-bridge/bridge.lock` so a second copy does not start by mistake.
- Runtime health is written to `/root/telegram-mtproto-bridge/bridge.health.json`.
- `healthcheck.sh` can be run manually or on a schedule later to restart the bridge if the process died or the heartbeat became stale.
- The script is intentionally small and uses only Telethon plus the Python standard library.
