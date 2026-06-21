"""信号持久化层 —— 基于 JSON 文件，支持多进程读写。"""

import json
import os
import threading
from datetime import datetime

# 信号文件路径（位于项目根目录）
SIGNAL_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "signals.json",
)

_lock = threading.Lock()


def load_all() -> list[dict]:
    """加载所有信号。"""
    if not os.path.exists(SIGNAL_FILE):
        return []
    with _lock:
        try:
            with open(SIGNAL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError):
            return []


def save_all(signals: list[dict]) -> None:
    """覆写全部信号到磁盘。"""
    with _lock:
        with open(SIGNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)


def add_signal(signal_data: dict) -> dict:
    """添加一条新信号。"""
    signals = load_all()
    signal_id = max((s.get("id", 0) for s in signals), default=0) + 1
    record = {
        "id": signal_id,
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": signal_data.get("symbol", ""),
        "direction": signal_data.get("direction", ""),
        "price": signal_data.get("price", 0),
        "score": signal_data.get("score", 0),
        "strategy": signal_data.get("strategy", ""),
        "status": signal_data.get("status", "ACTIVE"),
        "alert_count": signal_data.get("alert_count", 0),
        "trade_status": signal_data.get("trade_status", "待定"),
    }
    signals.append(record)
    if len(signals) > 200:
        signals = signals[-200:]
    save_all(signals)
    return record
