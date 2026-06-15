"""
Binance 合约交易所客户端（基于 ccxt）。

功能：查询余额、合约检查、持仓查询、K线数据、市价/限价开多、止盈止损。
"""
import logging
from typing import Any

import ccxt

from config.settings import Config

logger = logging.getLogger(__name__)


class ExchangeClient:
    """封装 ccxt 的 Binance USDⓈ-M 永续合约接口。"""

    def __init__(self, config: Config):
        self._cfg = config
        self._exch: ccxt.binanceusdm = self._构建交易所()
        self._markets_loaded = False

    # ── 公开方法 ─────────────────────────────────────────────

    def contract_exists(self, symbol: str) -> bool:
        """判断合约是否在 Binance 合约市场可交易。"""
        self._确保市场已加载()
        exists = symbol in self._exch.markets
        logger.info("合约 %s 在 Binance 合约市场%s",
                     symbol, "存在" if exists else "不存在")
        return exists

    def get_current_price(self, symbol: str) -> float | None:
        """获取合约最新标记价格。"""
        try:
            ticker = self._exch.fetch_ticker(symbol)
            price = ticker.get("markPrice") or ticker.get("last")
            if price:
                logger.info("合约 %s 当前标记价格: %.8f", symbol, price)
                return float(price)
        except Exception as exc:
            logger.error("获取 %s 价格失败: %s", symbol, exc)
        return None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "4h",
        limit: int = 100,
    ) -> list[list[float]]:
        """获取 K 线数据。

        返回值格式：``[[timestamp, open, high, low, close, volume], ...]``
        """
        try:
            data = self._exch.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            logger.info("获取 %s K线成功: 周期=%s 条数=%d", symbol, timeframe, len(data))
            return data
        except Exception as exc:
            logger.error("获取 %s K线失败 (%s): %s", symbol, timeframe, exc)
            return []

    def get_balance_usdt(self) -> float:
        """获取合约账户 USDT 余额（可用 + 冻结）。"""
        try:
            balance = self._exch.fetch_balance()
            total = float(balance.get("USDT", {}).get("total", 0))
            logger.info("合约账户 USDT 余额: %.2f", total)
            return total
        except Exception as exc:
            logger.error("获取余额失败: %s", exc)
            return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """设置合约杠杆倍数。"""
        try:
            self._exch.set_leverage(leverage, symbol)
            logger.info("合约 %s 杠杆设为 %dx", symbol, leverage)
        except Exception as exc:
            logger.warning("设置 %s 杠杆失败: %s", symbol, exc)

    def query_position(self, symbol: str) -> dict | None:
        """查询 *symbol* 的当前持仓。有持仓返回字典，无持仓返回 None。"""
        try:
            positions = self._exch.fetch_positions([symbol])
            for pos in positions:
                size = float(pos.get("contracts", 0) or pos.get("size", 0))
                if abs(size) > 0:
                    logger.info("发现 %s 持仓: 数量=%s 开仓价=%s",
                                symbol, size, pos.get("entryPrice", "N/A"))
                    return pos
        except Exception as exc:
            logger.warning("查询 %s 持仓失败: %s", symbol, exc)
        return None

    def has_open_position(self, symbol: str) -> bool:
        """判断 *symbol* 是否已有持仓。"""
        return self.query_position(symbol) is not None

    # ── 开仓与订单管理 ───────────────────────────────────────

    def open_long_market(
        self,
        symbol: str,
        quantity: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict[str, Any]:
        """市价开多，成交后自动挂止盈止损单。"""
        results: dict[str, Any] = {
            "entry": None, "stop_loss": None, "take_profit": None,
        }

        self.set_leverage(symbol, self._cfg.leverage)

        qty = float(self._exch.amount_to_precision(symbol, quantity))
        if qty <= 0:
            err_msg = f"数量 {quantity} 经精度舍入后为 {qty}，无法开仓"
            logger.error(err_msg)
            results["error"] = err_msg
            return results

        logger.info("正在市价开多 %s 数量=%s", symbol, qty)

        try:
            entry_order = self._exch.create_market_buy_order(symbol, qty)
            results["entry"] = entry_order
            logger.info(">>> 市价开仓成交: id=%s 价格=%s 数量=%s",
                        entry_order.get("id", "N/A"),
                        entry_order.get("price", "N/A"),
                        entry_order.get("filled", qty))
        except Exception as exc:
            err_msg = f"市价开仓失败 {symbol}: {exc}"
            logger.error(err_msg)
            results["error"] = err_msg
            return results

        # 成交后挂止盈止损
        self._挂止盈止损(symbol, qty, stop_loss_price, take_profit_price, results)

        return results

    def open_long_limit(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
    ) -> dict[str, Any]:
        """挂限价买入单（仅挂单，不设止盈止损）。返回订单信息。"""
        self.set_leverage(symbol, self._cfg.leverage)

        qty = float(self._exch.amount_to_precision(symbol, quantity))
        if qty <= 0:
            err_msg = f"数量 {quantity} 经精度舍入后为 {qty}，无法开仓"
            logger.error(err_msg)
            return {"error": err_msg}

        limit_price_rounded = float(self._exch.price_to_precision(symbol, limit_price))
        logger.info("正在挂限价买入 %s 数量=%s 价格=%s",
                     symbol, qty, limit_price_rounded)

        try:
            order = self._exch.create_limit_buy_order(symbol, qty, limit_price_rounded)
            logger.info(">>> 限价单已挂: id=%s 价格=%s 数量=%s",
                        order.get("id", "N/A"),
                        order.get("price", limit_price_rounded),
                        qty)
            return order
        except Exception as exc:
            err_msg = f"限价挂单失败 {symbol}: {exc}"
            logger.error(err_msg)
            return {"error": err_msg}

    def fetch_order_status(self, symbol: str, order_id: str) -> dict:
        """查询订单状态。"""
        try:
            order = self._exch.fetch_order(id=order_id, symbol=symbol)
            status = order.get("status", "unknown")
            filled = order.get("filled", 0)
            remaining = order.get("remaining", 0)
            logger.debug("订单 %s 状态=%s 已成交=%s 未成交=%s",
                         order_id, status, filled, remaining)
            return order
        except Exception as exc:
            logger.error("查询订单 %s 状态失败: %s", order_id, exc)
            return {"status": "error", "info": str(exc)}

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消订单。成功返回 True。"""
        try:
            self._exch.cancel_order(id=order_id, symbol=symbol)
            logger.info("订单 %s 已取消", order_id)
            return True
        except Exception as exc:
            logger.warning("取消订单 %s 失败: %s", order_id, exc)
            return False

    def set_stop_loss_take_profit(
        self,
        symbol: str,
        quantity: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict[str, Any]:
        """对已有持仓挂止盈止损单（reduced-only）。"""
        results: dict[str, Any] = {"stop_loss": None, "take_profit": None}

        qty = float(self._exch.amount_to_precision(symbol, quantity))

        self._挂止盈止损(symbol, qty, stop_loss_price, take_profit_price, results)
        return results

    # ── 资金费率 ──────────────────────────────────────────────

    def fetch_funding_rate(self, symbol: str) -> float:
        """获取 *symbol* 当前资金费率。"""
        try:
            funding = self._exch.fetch_funding_rate(symbol)
            rate = float(funding.get("fundingRate", 0) or 0)
            logger.debug("%s 当前资金费率: %.6f%%", symbol, rate * 100)
            return rate
        except Exception as exc:
            logger.warning("获取 %s 资金费率失败: %s", symbol, exc)
            return 0.0

    # ── 内部方法 ─────────────────────────────────────────────

    def _挂止盈止损(
        self,
        symbol: str,
        qty: float,
        sl_price: float,
        tp_price: float,
        results: dict,
    ) -> None:
        """向 *results* 字典写入止盈止损挂单结果。"""
        # 止损
        if sl_price > 0:
            try:
                sl_rounded = float(self._exch.price_to_precision(symbol, sl_price))
                sl_order = self._exch.create_order(
                    symbol=symbol,
                    type="STOP_MARKET",
                    side="SELL",
                    amount=qty,
                    params={"stopPrice": sl_rounded, "reduceOnly": True},
                )
                results["stop_loss"] = sl_order
                logger.info(">>> 止损挂单成功: 价格=%s 订单号=%s",
                            sl_rounded, sl_order.get("id", "N/A"))
            except Exception as exc:
                logger.error("止损挂单失败 %s: %s", symbol, exc)

        # 止盈
        if tp_price > 0:
            try:
                tp_rounded = float(self._exch.price_to_precision(symbol, tp_price))
                tp_order = self._exch.create_order(
                    symbol=symbol,
                    type="TAKE_PROFIT_MARKET",
                    side="SELL",
                    amount=qty,
                    params={"stopPrice": tp_rounded, "reduceOnly": True},
                )
                results["take_profit"] = tp_order
                logger.info(">>> 止盈挂单成功: 价格=%s 订单号=%s",
                            tp_rounded, tp_order.get("id", "N/A"))
            except Exception as exc:
                logger.error("止盈挂单失败 %s: %s", symbol, exc)

    def _构建交易所(self) -> ccxt.binanceusdm:
        exch = ccxt.binanceusdm({
            "apiKey": self._cfg.binance_api_key,
            "secret": self._cfg.binance_secret_key,
            "options": {"defaultType": "future"},
        })
        exch.enableRateLimit = True
        if self._cfg.binance_testnet:
            exch.set_sandbox_mode(True)
            logger.warning("Binance 合约测试网模式已启用 —— 订单为模拟执行")
        return exch

    def _确保市场已加载(self) -> None:
        if not self._markets_loaded:
            logger.info("正在从 Binance 合约加载市场数据 ...")
            self._exch.load_markets()
            self._markets_loaded = True
            logger.info("市场数据加载完成，共 %d 个交易对", len(self._exch.markets))
