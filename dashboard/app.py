"""
交易监控面板后端 —— Flask + SocketIO。
运行方式：
  python -m dashboard.app
"""
import json
import os
import sys
import queue
import time

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

from config.settings import load_config
from src.exchange import ExchangeClient
from src.position_service import PositionService
from dashboard import trade_store as ts
from dashboard import event_log as el
from dashboard import asset_history
import urllib.request
import urllib.error

app = Flask(__name__)
app.config["SECRET_KEY"] = "trade-dashboard-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# 全局交易所客户端
_exchange: ExchangeClient | None = None
_position_service: PositionService | None = None
_cfg = None

# ── 日志系统 ──
_log_buffer: list[dict] = []
MAX_LOGS = 500

_event_queue: queue.Queue = queue.Queue()

def add_log(level: str, message: str):
    """添加日志并广播到前端"""
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > MAX_LOGS:
        _log_buffer.pop(0)
    # 广播给所有连接的客户端
    socketio.emit("log", entry)


def _event_consumer():
    """消费进程内事件队列并广播到前端。"""
    while True:
        try:
            entry = _event_queue.get()
            add_log(entry.get("level", "INFO"), entry.get("message", ""))
        except Exception:
            pass


def _init_exchange():
    global _exchange, _cfg, _position_service
    _cfg = load_config()
    if not _cfg.is_valid:
        add_log("ERROR", "配置无效，请检查 .env 文件")
        return False
    _exchange = ExchangeClient(_cfg)
    _position_service = PositionService(_exchange)
    add_log("INFO", "交易所客户端初始化完成（测试网模式）")
    return True


def _fetch_dashboard_data() -> dict | None:
    """组装完整仪表盘数据 — 以交易所实时持仓为主，trade_store 补充历史。"""
    if _exchange is None:
        return None

    trades = ts.load_all()

    try:
        positions = _position_service.get_open_positions() if _position_service else []
    except Exception as e:
        add_log("WARN", f"获取持仓失败: {e}")
        positions = []

    live_map: dict = {}
    for pos in positions:
        sym = pos.get("symbol", "").strip()
        if not sym:
            continue
        size = float(pos.get("position_amt", 0) or 0)
        if abs(size) < 0.001:
            continue
        live_map[sym] = {
            "mark_price": float(pos.get("mark_price", 0) or 0),
            "unrealized_pnl": float(pos.get("unrealized_pnl", 0) or 0),
            "margin": float(pos.get("margin", 0) or 0),
            "position_size": size,
            "entry_price": float(pos.get("entry_price", 0) or 0),
            "liquidation_price": float(pos.get("liquidation_price", 0) or 0),
        }

    def _norm(s):
        return s.strip().upper().replace("/","").replace("-","").replace(" ","")

    merged = []
    live_normalized = set()

    for sym, live in live_map.items():
        d = "LONG" if live["position_size"] > 0 else "SHORT"
        merged.append({
            "id": "LIVE_" + sym,
            "symbol": sym,
            "direction": d,
            "entry_price": live["entry_price"],
            "mark_price": live["mark_price"],
            "margin": live["margin"],
            "unrealized_pnl": live["unrealized_pnl"],
            "status": "OPEN",
            "position_size": live["position_size"],
            "liquidation_price": live["liquidation_price"],
        })
        live_normalized.add(_norm(sym))

    for t in trades:
        t_sym = t.get("symbol", "") or ""
        if _norm(t_sym) in live_normalized:
            continue
        merged.append(dict(t))

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



@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    data = _fetch_dashboard_data()
    if data is None:
        return jsonify({"error": "初始化失败，请检查 .env 配置"}), 500
    # 同步获取账户余额
    try:
        acct = _position_service.get_account_info() if _position_service else {}
        usdt_total = float(acct.get("totalWalletBalance", 0))
        usdt_free = float(acct.get("availableBalance", 0))
        data["account"] = {
            "total_balance": round(usdt_total, 2),
            "available_balance": round(usdt_free, 2),
        }
    except Exception as e:
        add_log("ERROR", f"获取余额失败: {e}")
    return jsonify(data)


@app.route("/api/debug")
def api_debug():
    global _exchange
    if _exchange is None:
        return jsonify({"error": "not initialized"}), 500
    try:
        pos = [p["raw"] for p in _position_service.get_open_positions()] if _position_service else []
        sample = pos[:3] if pos else []
        return jsonify({
            "count": len(pos),
            "symbols": [p.get("symbol","") for p in pos],
            "sample_keys": list(sample[0].keys()) if sample else [],
            "sample": sample,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/account")
def api_account():
    """单独获取账户余额"""
    global _exchange
    if _exchange is None:
        return jsonify({"error": "not initialized"}), 500
    try:
        acct = _position_service.get_account_info()
        usdt_total = float(acct.get("totalWalletBalance", 0))
        usdt_free = float(acct.get("availableBalance", 0))
        # 计算未实现盈亏
        total_upnl = 0
        try:
            positions = _position_service.get_open_positions()
            for p in positions:
                upnl = float(p.get("unrealized_pnl", 0) or 0)
                total_upnl += upnl
        except:
            pass
        return jsonify({
            "total_balance": round(usdt_total, 2),
            "available_balance": round(usdt_free, 2),
            "unrealized_pnl": round(total_upnl, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/assets")
def api_assets():
    """获取合约账户所有资产余额明细。"""
    global _exchange
    if _exchange is None:
        return jsonify({"error": "not initialized"}), 500
    try:
        bals = _position_service.get_asset_balances()
        assets = []
        for b in bals:
            wb = float(b.get("wallet_balance", 0))
            if wb > 0:
                assets.append({
                    "asset": b.get("asset", ""),
                    "walletBalance": round(wb, 2),
                    "availableBalance": round(float(b.get("available_balance", 0)), 2),
                })
        total = sum(a["walletBalance"] for a in assets)
        return jsonify({"assets": assets, "total": round(total, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/signals")
def api_signals():
    """获取信号列表。"""
    from dashboard import signal_store as ss
    signals = ss.load_all()
    signals.reverse()
    return jsonify({"signals": signals, "count": len(signals)})


# ── SocketIO 事件 ──
@socketio.on("request_logs")
def on_request_logs():
    """客户端请求历史日志"""
    for entry in _log_buffer:
        emit("log", entry)

@socketio.on("ping")
def on_ping():
    emit("pong", {"time": time.strftime("%H:%M:%S")})


# ── 启动 ──────────────────────────────────────


def binance_speed_test(is_testnet=True):
    url = "https://testnet.binancefuture.com/fapi/v1/time" if is_testnet else "https://fapi.binance.com/fapi/v1/time"
    try:
        start_time = time.time()
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2) as response:
            if response.getcode() == 200:
                return int((time.time() - start_time) * 1000)
    except urllib.error.URLError as e:
        print(f"[测速警告] 币安连接失败 (Testnet={is_testnet}): {e}")
    except Exception as e:
        print(f"[测速错误]: {e}")
    return None

def bgcolor_heartbeat_thread():
    add_log("INFO", "心跳线程已启动")
    global _cfg
    print("[心跳线程] 等待交易引擎配置初始化...")
    while _cfg is None:
        time.sleep(0.5)
    add_log("INFO", "延迟测速开始")
    print("[心跳] ready")
    while True:
        try:
            is_mock = getattr(_cfg, 'BINANCE_TESTNET', True)
            delay = binance_speed_test(is_testnet=is_mock)
            # Binance delay logged via heartbeat event
            if delay is not None:
                socketio.emit('heartbeat', {'binance_delay': delay, 'is_mock': is_mock})
            else:
                socketio.emit('heartbeat', {'binance_delay': 999, 'is_mock': is_mock})
        except Exception as err:
            print(f"[心跳线程异常]: {err}")
        time.sleep(3)  # 15分钟


def _event_poller():
    last = 0
    last_mtime = 0
    while True:
        try:
            try:
                current_mtime = os.path.getmtime(el.EVENT_FILE)
            except Exception:
                current_mtime = 0
            if current_mtime == last_mtime:
                time.sleep(5)
                continue
            last_mtime = current_mtime
            evts = el.read_all()
            if len(evts) > last:
                for e in evts[last:]:
                    tp = e.get("type", "INFO")
                    lv = {"信号": "信号", "开仓": "开仓", "未开": "未开", "分析": "分析"}.get(tp, "INFO")
                    add_log(lv, "[" + tp + "] " + e.get("symbol", "") + " " + e.get("detail", ""))
                last = len(evts)
        except:
            pass
        time.sleep(5)


def _asset_snapshot_loop():
    """每 60 秒记录一次资产快照到 asset_history.json。"""
    while True:
        time.sleep(60)
        try:
            acct = _position_service.get_account_info()
            usdt_total = float(acct.get('totalWalletBalance', 0))
            usdt_free = float(acct.get('availableBalance', 0))
            total_upnl = 0.0
            try:
                positions = _position_service.get_open_positions()
                total_upnl = sum(float(p.get('unrealized_pnl', 0) or 0) for p in positions)
            except Exception:
                pass
            asset_history.record_snapshot(usdt_total, usdt_free, total_upnl)
            add_log("INFO", f"资产快照: 余额={usdt_total:.2f} USDT")
        except Exception as e:
            add_log("WARN", f"资产快照失败: {e}")

def main():
    if not _init_exchange():
        print("[仪表盘] 无法初始化交易所客户端，请检查 .env 文件")
        sys.exit(1)

    print(f"[仪表盘] 交易记录文件: {ts.TRADE_FILE}")
    add_log("INFO", f"交易记录文件: {ts.TRADE_FILE}")
    add_log("INFO", "仪表盘就绪，等待数据加载...")
    print("[仪表盘] 就绪，等待手动刷新 ...")

    socketio.start_background_task(_event_consumer)
    socketio.start_background_task(_asset_snapshot_loop)
    socketio.start_background_task(bgcolor_heartbeat_thread)
    socketio.start_background_task(_event_poller)

    print("[仪表盘] 启动 Web 服务: http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
