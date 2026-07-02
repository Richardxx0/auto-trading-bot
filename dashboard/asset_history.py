"""Asset history module - periodic balance snapshots for the trend chart."""
import json
import os
import time
from threading import Lock
HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "asset_history.json",
)
_lock = Lock()
MAX_RECORDS = 18000

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with _lock:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError):
            return []

def save_history(records):
    with _lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

def record_snapshot(total_balance, available_balance, total_upnl):
    records = load_history()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    equity = total_balance + total_upnl
    records.append({
        "time": now,
        "balance": round(total_balance, 2),
        "available": round(available_balance, 2),
        "upnl": round(total_upnl, 4),
        "equity": round(equity, 2),
    })
    if len(records) > MAX_RECORDS:
        records = records[-MAX_RECORDS:]
    save_history(records)

def get_recent(limit=720):
    records = load_history()
    return records[-limit:] if len(records) > limit else records
