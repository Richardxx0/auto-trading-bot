"""
交易监控面板后端 —— Flask + SocketIO。
运行方式：
  python -m dashboard.app
"""
import json
import os
import sys
import time

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO

from config.settings import load_config
from src.exchange import ExchangeClient
from dashboard import trade_store as ts

app = Flask(__name__)
app.config["SECRET_KEY"] = "trade-dashboard-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# 全局交易所客户端
_exchange: ExchangeClient | None = None
_cfg = None


def _init_exchange():
    global _exchange, _cfg
    _cfg = load_config()
    if not _cfg.is_valid:
        print("[仪表盘] 配置无效，请检查 .env 文件")
        return False
    _exchange = ExchangeClient(_cfg)
    return True


def _fetch_dashboard_data() -> dict | None:
    """组装完整仪表盘数据。"""
    if _exchange is None:
        if not _init_exchange():
            return None

    trades = ts.load_all()

    # 从交易所获取当前持仓的实时数据
    try:
        positions = _exchange._exch.fetch_positions() if _exchange else []
    except Exception:
        positions = []

    # 构建持仓实时数据索引（symbol → live data）
    live_map: dict = {}
    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym:
            continue
        size = float(pos.get("contracts", 0) or pos.get("size", 0))
        if abs(size) < 0.001:
            continue
        live_map[sym] = {
            "mark_price": float(pos.get("markPrice", 0) or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
            "margin": float(pos.get("initialMargin", 0) or 0),
            "position_size": size,
            "entry_price": float(pos.get("entryPrice", 0) or 0),
            "liquidation_price": float(pos.get("liquidationPrice", 0) or 0),
        }

    # 合并交易记录与实时数据
    merged = []
    for t in trades:
        sym = t.get("symbol", "")
        live = live_map.get(sym, None)
        row = dict(t)

        if live:
            row["mark_price"] = live["mark_price"]
            row["unrealized_pnl"] = live["unrealized_pnl"]
            row["margin"] = live["margin"]
            row["position_size"] = live["position_size"]
            row["liquidation_price"] = live["liquidation_price"]
        else:
            row["mark_price"] = None
            row["unrealized_pnl"] = None
            row["margin"] = None
            row["position_size"] = None
            row["liquidation_price"] = None

        merged.append(row)

    # 统计汇总
    total_pnl = sum(
        float(r.get("unrealized_pnl", 0) or 0)
        + float(r.get("realized_pnl", 0) or 0)
        for r in merged
    )
    open_count = sum(1 for r in merged if r.get("status") == "OPEN")
    total_margin = sum(
        float(r.get("margin", 0) or 0) for r in merged if r.get("status") == "OPEN"
    )

    return {
        "trades": merged,
        "summary": {
            "total_pnl": round(total_pnl, 4),
            "open_count": open_count,
            "total_margin": round(total_margin, 4),
            "total_trades": len(merged),
        },
        "timestamp": time.strftime("%H:%M:%S"),
    }


# ── Flask 路由 ────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    data = _fetch_dashboard_data()
    if data is None:
        return jsonify({"error": "初始化失败，请检查 .env 配置"}), 500
    return jsonify(data)


# ── 启动 ──────────────────────────────────────

def main():
    if not _init_exchange():
        print("[仪表盘] 无法初始化交易所客户端，请检查 .env 文件")
        sys.exit(1)

    print(f"[仪表盘] 交易记录文件: {ts.TRADE_FILE}")
    print("[仪表盘] 就绪，等待手动刷新 ...")

    print("[仪表盘] 启动 Web 服务: http://127.0.0.1:5000")
    socketio.run(app, host="127.0.0.1", port=5000, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
