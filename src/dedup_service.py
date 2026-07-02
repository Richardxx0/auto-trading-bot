"""Dedup Service — 两层去重持久化层。

格式迁移：
  旧: {"SYMBOL": timestamp}              → 所有条目 status=OPEN
  新: {"SYMBOL": {"direction": "", "alert_count": 0, "status": "OPEN|CLOSED", "updated_at": "..."}}
"""

import json
import os
import threading
from datetime import datetime
from typing import Any

DEDUP_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dedup.json",
)

_lock = threading.RLock()


def load() -> dict[str, dict[str, Any]]:
    """加载 dedup 文件，自动迁移旧格式。"""
    if not os.path.exists(DEDUP_FILE):
        return {}
    with _lock:
        try:
            with open(DEDUP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            # 迁移旧格式 {"SYM": timestamp}
            if data and isinstance(next(iter(data.values())), (int, float)):
                new_data = {}
                for sym, ts_raw in data.items():
                    try:
                        ts = datetime.fromtimestamp(float(ts_raw))
                    except (ValueError, OSError):
                        ts = datetime.now()
                    new_data[sym] = {
                        "direction": "",
                        "alert_count": 0,
                        "status": "OPEN",
                        "updated_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                data = new_data
                save(data)
                return data
            return data
        except (json.JSONDecodeError, IOError):
            return {}


def save(data: dict[str, dict[str, Any]]) -> None:
    """写入 dedup 文件。"""
    with _lock:
        with open(DEDUP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def update_entry(
    symbol: str,
    direction: str = "",
    alert_count: int = 0,
    status: str = "OPEN",
) -> None:
    """更新单条记录并持久化。"""
    data = load()
    data[symbol] = {
        "direction": direction,
        "alert_count": alert_count,
        "status": status,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save(data)


def set_status(symbol: str, status: str = "CLOSED") -> None:
    """修改单条记录的状态字段。"""
    data = load()
    if symbol in data:
        data[symbol]["status"] = status
        data[symbol]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save(data)


def get_active_symbols() -> set[str]:
    """获取所有状态为 OPEN 的币种集合。"""
    data = load()
    return {s for s, e in data.items() if e.get("status") == "OPEN"}
