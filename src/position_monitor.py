"""持仓监控器 —— 后台管理移动止损。"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class PositionMonitor:
    """后台任务：监控持仓，管理移动止损。"""

    def __init__(self, exchange_client, config):
        self._exch = exchange_client
        self._cfg = config
        self._running = False
        self._high_prices: dict[str, float] = {}

    async def start(self):
        self._running = True
        logger.info("持仓监控器已启动（检查间隔30秒）")
        while self._running:
            try:
                self._check()
            except Exception as exc:
                logger.exception("持仓监控异常: %s", exc)
            await asyncio.sleep(30)

    def stop(self):
        self._running = False

    def _check(self):
        try:
            positions = self._exch._exch.fetch_positions()
        except Exception as exc:
            logger.warning("获取持仓失败: %s", exc)
            return
        for pos in positions:
            symbol = pos.get("symbol", "")
            size = float(pos.get("contracts", 0) or pos.get("size", 0))
            entry = float(pos.get("entryPrice", 0) or 0)
            mark = float(pos.get("markPrice", 0) or 0)
            if abs(size) < 0.001:
                self._high_prices.pop(symbol, None)
                continue
            if entry <= 0 or mark <= 0:
                continue
            prev_high = self._high_prices.get(symbol, entry)
            curr_high = max(prev_high, mark)
            self._high_prices[symbol] = curr_high
            pnl_pct = (mark - entry) / entry
            activation = self._cfg.trailing_stop_activation_pct
            trail_dist = self._cfg.trailing_stop_distance_pct
            if pnl_pct < activation or curr_high <= entry:
                continue
            # ATR-based trailing stop (if configured)
            if self._cfg.trailing_stop_atr_multiplier > 0:
                try:
                    candles = self._exch._exch.fetch_ohlcv(symbol, "4h", 20)
                    if candles and len(candles) > 15:
                        highs = [c[2] for c in candles[-15:]]
                        lows = [c[3] for c in candles[-15:]]
                        closes = [c[4] for c in candles[-15:]]
                        trs = []
                        for i in range(1, len(candles[-15:])):
                            tr = max(highs[i] - lows[i],
                                     abs(highs[i] - closes[i-1]),
                                     abs(lows[i] - closes[i-1]))
                            trs.append(tr)
                        atr_val = sum(trs) / len(trs)
                        new_sl = curr_high - atr_val * self._cfg.trailing_stop_atr_multiplier
                        logger.info("  ATR距离=%.4f ATR倍数=%.1f 新SL=%.6f",
                                    atr_val / curr_high,
                                    self._cfg.trailing_stop_atr_multiplier,
                                    new_sl)
                    else:
                        new_sl = curr_high * (1 - trail_dist)
                except Exception:
                    new_sl = curr_high * (1 - trail_dist)
            else:
                new_sl = curr_high * (1 - trail_dist)
            self._exch.cancel_all_stop_loss(symbol)
            qty = float(self._exch._exch.amount_to_precision(symbol, abs(size)))
            oid = self._exch.place_stop_loss_order(symbol, qty, new_sl)
            if oid:
                logger.info("\u79fb\u52a8\u6b62\u635f: %s \u65b0\u9ad8=%.6f(+%.1f%%) SL=%.6f",
                            symbol, curr_high,
                            (curr_high / entry - 1) * 100, new_sl)
