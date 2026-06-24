"""
Position Monitor - price milestone state machine.

Logic:
  profit>=6%   -> breakeven SL (entry*1.002)
  profit>=10%  -> SL raised to +4%
"""
import asyncio
import logging
from core.exchange_service import ExchangeService
from dashboard import trade_store as ts
logger = logging.getLogger(__name__)

class PositionMonitor:
    def __init__(self, exchange_service, config):
        self._exch_service = exchange_service
        self._exch = exchange_service._exch
        self._cfg = config
        self._running = False
        self._sl_stage: dict[str, int] = {}
        self._checking = False
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
                return self._exch._exch.fapiPrivateV2GetPositionRisk()
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
            cur_qty = float(pos.get("positionAmt", 0) or 0)
            mark = float(pos.get("markPrice", 0) or 0)
            if abs(cur_qty) < 0.001 or mark <= 0:
                continue
            trade = trade_map.get(symbol)
            if not trade:
                continue
            entry = trade.get("entry_price", 0) or float(pos.get("entryPrice", 0) or 0)
            if entry <= 0:
                continue

            # price milestone state machine
            current_profit = (mark - entry) / entry
            stage = self._sl_stage.get(symbol, 0)
            new_sl = None

            if current_profit >= 0.20 and stage < 4:
                new_sl = entry * 1.20
                self._sl_stage[symbol] = 4
                logger.info("[RUNNER] %s profit+%.1f%% -> final stage, SL locked at +20%%, cancel take-profits",
                            symbol, current_profit * 100)
                try:
                    await self._exch_service.cancel_all_take_profits(symbol)
                    ts.update_trade(trade["id"], runner=True)
                except Exception:
                    pass

            elif current_profit >= 0.15 and stage < 3:
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

        # cache last known position data for close_trade
        for pos in positions:
            s = pos.get("symbol", "")
            q = float(pos.get("positionAmt", 0) or 0)
            if abs(q) > 0.001:
                side = pos.get("positionSide")
                if not side:
                    side = "LONG" if q > 0 else "SHORT"
                self._last_pos_data[(s, side.upper())] = {
                    "close_price": float(pos.get("markPrice", 0) or 0),
                    "unrealized_pnl": float(pos.get("unrealizedProfit", 0) or 0),
                }

        # ================== 双重防线清理逻辑 ==================
        # 1. 提取币安实时的持仓镜像，同时记录实时入场价
        active_positions_info = {}
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) > 0.001:
                side = p.get("positionSide")
                if not side:
                    side = "LONG" if amt > 0 else "SHORT"
                s = p["symbol"]
                active_positions_info[(s, side.upper())] = float(p.get("entryPrice", 0) or 0)

        # 2. 遍历本地 OPEN 记录，执行方向 + 入场价双重校验
        for t in trades:
            s = t.get("symbol", "")
            t_side = (t.get("direction") or t.get("side") or "LONG").upper()
            t_entry = float(t.get("entry_price", 0) or 0)

            key = (s, t_side)
            should_cleanup = False
            reason = ""

            if key not in active_positions_info:
                should_cleanup = True
                reason = "交易所已无持仓"
            else:
                binance_entry = active_positions_info[key]
                if t_entry > 0 and binance_entry > 0:
                    deviation = abs(binance_entry - t_entry) / t_entry
                    if deviation > 0.005:
                        should_cleanup = True
                        reason = "同向换批次(本地:" + str(t_entry) + " vs 交易所:" + str(binance_entry) + ")"

            if should_cleanup:
                ldata = self._last_pos_data.get(key, {})
                ts.close_trade(t["id"], realized_pnl=ldata.get("unrealized_pnl", 0.0), close_price=ldata.get("close_price", 0.0))
                if s in self._sl_stage:
                    del self._sl_stage[s]
                logger.info("[CLEANUP] 成功清理脱节记录: " + s + " " + t_side + " | 原因: " + reason)

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
