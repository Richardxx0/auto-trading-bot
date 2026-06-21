"""共享事件日志 —— 跨进程读写 events.json，供仪表盘展示。"""

import json
import os
import threading
from datetime import datetime

# 事件文件路径（项目根目录）
EVENT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "events.json",
)

_lock = threading.Lock()


def write(event_type: str, symbol: str, detail: str = ""):
    """写一条事件到 events.json。"""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": event_type,
        "symbol": symbol,
        "detail": detail,
    }
    with _lock:
        events = []
        if os.path.exists(EVENT_FILE):
            try:
                with open(EVENT_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except (json.JSONDecodeError, IOError):
                events = []
        events.append(entry)
        if len(events) > 500:
            events = events[-500:]
        with open(EVENT_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    return entry


def read_all() -> list[dict]:
    """读取所有事件。"""
    if not os.path.exists(EVENT_FILE):
        return []
    with _lock:
        try:
            with open(EVENT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []


def clear():
    """清空事件文件。"""
    with _lock:
        with open(EVENT_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
