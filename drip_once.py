#!/usr/bin/env python3
import asyncio, json, os, subprocess, sys
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
CUTOFF = datetime(2026, 9, 12, 20, 13, tzinfo=timezone.utc)
STATE = os.path.join(DIR, "rss-state.json")
PROGRESS = os.path.join(DIR, "drip-progress.json")

def load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d

def save(p, v):
    json.dump(v, open(p, "w"))
async def main():
    import bridge
    from email.utils import parsedate_to_datetime
    items = await bridge.parse_rss_items("http://domdara.org/feed/")
    if not items:
        print("drip: feed empty", flush=True)
        return
    newest_id = items[0].get("id", "")
    state = load(STATE, {})
    released = set(load(PROGRESS, []))
    backlog = []
    for it in items:
        try:
            ts = parsedate_to_datetime(it.get("published", "")).astimezone(timezone.utc)
        except Exception:
            continue
        if ts < CUTOFF and it.get("id", "") not in released:
            backlog.append((ts, it))
    if not backlog:
        print("drip: drained, restarting daemon", flush=True)
        subprocess.run(["./start.sh"], cwd=DIR, capture_output=True)
        subprocess.run("crontab -l 2>/dev/null | grep -v drip_once | crontab -", shell=True)
        print("drip: daemon back, cron removed", flush=True)
        return
    backlog.sort()
    ts, victim = backlog[0]
    vkeys = set(bridge.item_keys(victim))
    allkeys = set(load(STATE, {}).get("recent_ids", []))
    for it in items:
        allkeys.update(bridge.item_keys(it))
    state["recent_ids"] = sorted(allkeys - vkeys)
    state["last_seen_id"] = newest_id
    save(STATE, state)
    print("drip: released", victim.get("id", ""), victim.get("title", "")[:60], flush=True)
    r = subprocess.run(["./.venv/bin/python", "./bridge.py", "--once"],
                       cwd=DIR, capture_output=True, text=True, timeout=280)
    print("drip: once rc=", r.returncode, (r.stdout + r.stderr)[-400:], flush=True)
    st2 = load(STATE, {})
    if vkeys & set(st2.get("recent_ids", [])):
        released.add(victim.get("id", ""))
        save(PROGRESS, sorted(released))
        print("drip: posted, progress", len(released), flush=True)
    else:
        print("drip: NOT confirmed, retry next hour", flush=True)

asyncio.run(main())
