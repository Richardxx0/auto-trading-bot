"""交易记录持久化层 —— 基于 JSON 文件，支持多进程读写。"""

import json
import os
import threading
from datetime import datetime
from typing import Any

# 交易记录文件路径（位于项目根目录）
TRADE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trades.json",
)

_lock = threading.Lock()


def normalize_symbol(symbol: str) -> str:
    """标准化为 Binance 合约格式（如 BTCUSDT），在所有入口处强制统一。"""
    s = symbol.upper().strip()
    if "/" in s:
        s = s.replace("/", "")
    if "-" in s:
        s = s.replace("-", "")
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def load_all() -> list[dict]:
    """加载所有交易记录。"""
    if not os.path.exists(TRADE_FILE):
        return []
    with _lock:
        try:
            with open(TRADE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError):
            return []


def save_all(trades: list[dict]) -> None:
    """覆写全部交易记录到磁盘。"""
    with _lock:
        with open(TRADE_FILE, "w", encoding="utf-8") as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)


def add_trade(trade_data: dict) -> dict:
    """添加一条新交易记录。"""
    trades = load_all()
    trade_id = max((t.get("id", 0) for t in trades), default=0) + 1
    record: dict[str, Any] = {
        "id": trade_id,
        "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "close_time": None,
        "status": "OPEN",
        "realized_pnl": 0.0,
        "funding_fee": 0.0,
    }
    record.update(trade_data)
    # 在写入前强制标准化 symbol
    if "symbol" in record:
        record["symbol"] = normalize_symbol(record["symbol"])
    trades.append(record)
    save_all(trades)
    return record


def update_trade(trade_id: int, **updates) -> dict | None:
    """更新指定交易记录的字段。"""
    trades = load_all()
    for t in trades:
        if t.get("id") == trade_id:
            t.update(updates)
            save_all(trades)
            return t
    return None


def close_trade(
    trade_id: int,
    realized_pnl: float | None = None,
    close_price: float | None = None,
    close_reason: str = "SYNC",
) -> dict | None:
    """标记交易为已平仓。"""
    updates: dict[str, Any] = {
        "status": "CLOSED",
        "close_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "close_reason": close_reason,
    }
    if realized_pnl is not None:
        updates["realized_pnl"] = round(realized_pnl, 4)
    if close_price is not None:
        updates["close_price"] = close_price
    return update_trade(trade_id, **updates)
