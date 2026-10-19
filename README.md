# Telegram / RSS-мост

**Telegram MTProto Bridge** — инструмент для передачи материалов из
согласованной Telegram-группы и RSS-ленты в выбранные Telegram-каналы.
Подходит для процесса с заранее определёнными источниками и правилами отбора.

**Основа для адаптации публикаций:** текст и подписи, медиаальбомы,
фильтр по автору и опрос RSS. Источники, правила и каналы назначения
настраиваются под ваш процесс.

## Сценарий

1. Согласовать доступные источники, получателей и право повторной публикации.
2. Настроить источники и фильтр; проверить отбор на тестовых материалах.
3. Передать подходящие материалы в выбранные каналы и проверить результат.

<details>
<summary>Настройка и ограничения</summary>

- Используется **пользовательская Telegram-сессия** с доступом к источнику
  и правом публикации в целевом канале, а не только Bot API.
- Запуск с рабочей сессией может публиковать от вашего аккаунта. Первый
  прогон проводите на согласованных тестовых источниках и канале.
- Фильтр автора отбирает по ID. Дополнительные правила, повторы и поведение
  после перезапуска проверяются для выбранного сценария.
- Доступ к источникам и право повторной публикации материалов согласуются
  заранее. Секреты и файлы сессии нельзя публиковать.
- В дереве этой версии нет отдельного LICENSE. Права на использование и
  передачу требуют уточнения; английская инструкция ниже сохранена.

</details>

[Возможности](#what-it-does) · [Требования](#requirements) ·
[Запуск](#setup) · [Технические заметки](#notes) ·
[Портфолио и связь](https://sereban-glitch.github.io/#contact)

Ниже сохранена техническая инструкция на английском языке.

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
- The script is intentionally small and uses only Telethon plus the Python standard library.

## Связанные проекты / Интеграция с видеоконвейером

Логика опроса RSS (`http://domdara.org/feed/`), дедупликации и извлечения номеров судебных дел (`bridge.py`) используется в автономном видеоконвейере **[Tubecook v4](https://github.com/Sereban-glitch/tubecook)** для автоматической генерации Shorts/Reels/Telegram-видео:
- Спецификация адаптера сбора: [SPEC_DOMDARA_INGEST.md](https://github.com/Sereban-glitch/tubecook/blob/main/docs/SPEC_DOMDARA_INGEST.md)
- Памятка состояния и аппаратных замеров: [MEMO_STATUS.md](https://github.com/Sereban-glitch/tubecook/blob/main/docs/MEMO_STATUS.md)
