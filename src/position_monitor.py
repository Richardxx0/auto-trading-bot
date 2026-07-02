"""
Position Monitor - price milestone state machine.

Logic:
  profit>=6%   -> breakeven SL (entry*1.002)
  profit>=10%  -> SL raised to +4%
"""
import asyncio
import time
from dashboard import event_log as el
import logging
from core.exchange_service import ExchangeService
from dashboard import trade_store as ts
from src.position_service import PositionService
from src.trade_constants import CloseReason
from src import dedup_service
logger = logging.getLogger(__name__)

class PositionMonitor:
    def __init__(self, exchange_service, config, position_service: PositionService | None = None):
        self._exch_service = exchange_service
        self._exch = exchange_service._exch
        self._cfg = config
        self._pos_service = position_service or PositionService(self._exch)
        self._running = False
        self._sl_stage: dict[str, int] = {}
        self._checking = False
        self._last_health_count: int = -1
        self._last_pos_data: dict = {}

    async def start(self):
        self._running = True
        logger.info("PosMon started (10s)")
        while self._running:
            try:
                await self._check()
            except Exception as exc:
                logger.exception("PosMon error: " + str(exc))
            await asyncio.sleep(10)

    def stop(self):
        self._running = False

    async def _check(self):
        if self._checking:
            return
        self._checking = True
        try:
            def _fetch():
                return self._pos_service.get_open_positions()
            positions = await self._exch_service.run(
                _fetch, timeout=self._exch_service.TIMEOUTS["position"])
        except Exception as exc:
            logger.warning("Fetch positions failed: " + str(exc))
            return
        finally:
            self._checking = False

        trades = [t for t in ts.load_all() if t.get("status") == "OPEN"]
        trade_map = {t["symbol"]: t for t in trades}

        for pos in positions:
            symbol = pos.get("symbol", "")
            cur_qty = abs(float(pos.get("position_amt", 0) or 0))
            mark = float(pos.get("mark_price", 0) or 0)
            if abs(cur_qty) < 0.001 or mark <= 0:
                continue
            trade = trade_map.get(symbol)
            if not trade:
                continue
            entry = trade.get("entry_price", 0) or float(pos.get("entry_price", 0) or 0)
            if entry <= 0:
                continue

            # price milestone state machine
            current_profit = (mark - entry) / entry
            stage = self._sl_stage.get(symbol, 0)
            new_sl = None

            # [RUNNER REMOVED] lottery vault disabled, batch TP 50/30/20 only










            if current_profit >= 0.15 and stage < 3:
                new_sl = entry * 1.10
                self._sl_stage[symbol] = 3
                logger.info("[TP2-LOCK] %s profit+%.1f%% -> SL raised to +10%%",
                            symbol, current_profit * 100)

            elif current_profit >= 0.10 and stage < 2:
                new_sl = entry * 1.04
                self._sl_stage[symbol] = 2
                logger.info("[TP1-LOCK] %s profit+%.1f%% -> SL raised to +4%%",
                            symbol, current_profit * 100)

            elif current_profit >= 0.06 and stage < 1:
                new_sl = entry * 1.002
                self._sl_stage[symbol] = 1
                logger.info("[BREAK-EVEN] %s profit+%.1f%% -> SL at breakeven+0.2%%",
                            symbol, current_profit * 100)

            if new_sl and new_sl > 0:
                await self._update_sl(symbol, abs(cur_qty), new_sl, current_profit)

        # ── 仓位健康检查 ─────────────────────────────
        from datetime import datetime
        # 仓位利用率日志（仅在变化时打印）
        cur = len(trades)
        if cur != self._last_health_count:
            logger.info("[HEALTH] 当前持仓: %d/%d (变化 %s%d)",
                        cur, self._cfg.max_open_positions,
                        "+" if cur > self._last_health_count else "",
                        cur - self._last_health_count if self._last_health_count >= 0 else 0)
            self._last_health_count = cur
        now_t = time.time()
        pos_by_sym = {p["symbol"]: p for p in positions}

        for t in trades:
            sym = t.get("symbol", "")
            if not sym:
                continue
            ot = t.get("open_time", "")
            if not ot:
                continue
            try:
                hold_h = (now_t - datetime.strptime(ot, "%Y-%m-%d %H:%M:%S").timestamp()) / 3600
            except:
                continue

            p = pos_by_sym.get(sym)
            if not p:
                continue
            notional = abs(p["position_amt"]) * p["mark_price"]
            if notional < 1:
                continue
            pnl_pct = p["unrealized_pnl"] / notional

            reason = None
            if hold_h >= self._cfg.health_timeout_loss_hours and pnl_pct <= self._cfg.health_timeout_loss_pct:
                reason = CloseReason.TIMEOUT_LOSS
            elif hold_h >= self._cfg.health_timeout_hours:
                reason = CloseReason.TIMEOUT

            if not reason:
                continue
            try:
                self._exch.close_position_full(sym)
                ts.close_trade(t["id"], realized_pnl=None, close_price=None, close_reason=reason)
                dedup_service.set_status(sym, "CLOSED")
                logger.info("[HEALTH] %s: %s 已平仓 (%.1fh, %.2f%%)",
                            reason, sym, hold_h, pnl_pct * 100)
                el.write("健康检查", sym, "%s %.1fh %.2f%%%%" % (reason, hold_h, pnl_pct * 100))
            except Exception as e:
                logger.warning("[HEALTH] %s 平仓失败，下一轮重试: %s", sym, e)

        if self._pos_service:
            try:
                self._pos_service.sync_positions()
            except Exception as exc:
                logger.warning("sync_positions failed: " + str(exc))

    async def _update_sl(self, symbol, qty, new_sl, pnl_pct):
        try:
            await self._exch_service.cancel_all_stop_loss(symbol)
            q = float(await self._exch_service.amount_to_precision(symbol, qty))
            p = self._round_price(symbol, new_sl)
            oid = await self._exch_service.place_stop_loss_order(symbol, q, p)
            if oid:
                logger.info(f"Trail SL: {symbol} price={new_sl} pnl+{round(pnl_pct*100,1)}pct order={oid}")
        except Exception as exc:
            logger.error("Update SL failed " + symbol + ": " + str(exc))

    def _round_price(self, symbol, price):
        try:
            return float(self._exch._exch.price_to_precision(symbol, price))
        except:
            return round(price, 4)
