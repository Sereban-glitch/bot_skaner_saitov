import asyncio
import os
from pathlib import Path
from telethon import TelegramClient
from telethon.tl.functions.messages import GetForumTopicsRequest

def env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))

async def main():
    config_dir = Path('/home/u0_a566/bot_skaner_saitov')
    load_dotenv(config_dir / '.env')
    
    session_path = str(config_dir / 'analytics.session')
    client = TelegramClient(session_path, int(env('API_ID')), env('API_HASH'))
    await client.start()
    
    try:
        channel_ref = '@ludolov_zp'
        entity = await client.get_entity(channel_ref)
        print(f"Got entity: {entity.title}, ID: {entity.id}, Forum: {getattr(entity, 'forum', False)}")
        
        if getattr(entity, 'forum', False):
            # Fetch topics
            topics = await client(GetForumTopicsRequest(
                peer=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100
            ))
            
            for topic in topics.topics:
                title = getattr(topic, 'title', 'Unknown')
                print(f"Topic: {topic.id} - {title}")
                if "Ситуация" in title or "ситуация" in title.lower():
                    print(f"===> FOUND TARGET TOPIC ID: {topic.id}")
                    
                    # fetch a few messages
                    msgs = await client.get_messages(entity, limit=5, reply_to=topic.id)
                    for m in msgs:
                        text = m.text.replace('\n', ' ') if m.text else ''
                        print(f"Msg {m.id} at {m.date}: {text[:50]}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
