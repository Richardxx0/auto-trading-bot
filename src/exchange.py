"""
Binance 合约交易所客户端（基于 ccxt）。

功能：查询余额、合约检查、持仓查询、K线数据、市价/限价开多、止盈止损。
"""
import logging
from typing import Any

import requests
import ccxt

from config.settings import Config

logger = logging.getLogger(__name__)


class ExchangeClient:
    """封装 ccxt 的 Binance USDⓈ-M 永续合约接口。"""

    def __init__(self, config: Config):
        self._cfg = config
        self._exch = self._构建交易所()
        self._markets_loaded = False

    # ── 公开方法 ─────────────────────────────────────────────

    def contract_exists(self, symbol: str) -> bool:
        """判断合约是否在 Binance 合约市场可交易。"""
        try:
            info = self._exch.fapiPublicGetExchangeInfo()
            symbols = info.get("symbols", [])
            exists = any(s.get("symbol") == symbol for s in symbols if s.get("status") == "TRADING")
            logger.info("合约 %s 在 Binance 合约市场%s",
                         symbol, "存在" if exists else "不存在")
            return exists
        except Exception:
            return False

    def get_current_price(self, symbol: str) -> float | None:
        """获取合约最新标记价格。"""
        try:
            self._确保市场已加载()
            ticker = self._exch.fapiPublicGetTickerPrice(params={"symbol": symbol})
            if isinstance(ticker, list):
                ticker = ticker[0] if ticker else {}
            price = ticker.get("price") or ticker.get("markPrice") or ticker.get("lastPrice")
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
            self._确保市场已加载()
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

    def _cap_qty(self, symbol: str, raw_qty: float) -> float:
        try:
            m = self._exch.markets_by_id.get(symbol)
            if isinstance(m, list) and m:
                m = m[0]
            if not m:
                m = self._exch.markets_by_id.get(symbol)
                if isinstance(m, list) and m:
                    m = m[0]
            if not m:
                return raw_qty
            info = m.get('info', {})
            filters = info.get('filters', [])
            max_qty = 999999999.0
            min_qty = 0.0
            for f in filters:
                ft = f.get('filterType', '')
                if ft in ('MARKET_LOT_SIZE', 'LOT_SIZE'):
                    max_qty = float(f.get('maxQty', max_qty))
                    min_qty = float(f.get('minQty', min_qty))
                    if ft == 'MARKET_LOT_SIZE':
                        break
            capped = max(min(raw_qty, max_qty), min_qty)
            if capped < raw_qty:
                logger.warning('  %s number %.4f > max allowable %.4f, clipped', symbol, raw_qty, max_qty)
            return capped
        except Exception as exc:
            logger.warning('  check %s number cap failed: %s', symbol, exc)
            return raw_qty

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

# 止损管理方法
    def get_open_positions_count(self) -> int:
        try:
            positions = self._exch.fapiPrivateV2GetPositionRisk()
            return sum(1 for p in positions
                      if abs(float(p.get("positionAmt", 0) or 0)) > 0.01)
        except Exception:
            return 0

    def place_stop_loss_order(self, symbol: str, qty: float, sl_price: float) -> str | None:
        try:
            qty_r = float(self._exch.amount_to_precision(symbol, qty))
            sl_r = float(self._exch.price_to_precision(symbol, sl_price))
            order = self._exch.create_order(
                symbol=symbol, type="STOP_MARKET", side="SELL",
                amount=qty_r,
                params={"stopPrice": sl_r, "positionSide": "LONG"},
            )
            return order.get("id")
        except Exception as exc:
            print(f"\u635f\u6b62\u6302\u5355\u5931\u8d25 {symbol}: {exc}")
            return None

    def cancel_all_stop_loss(self, symbol: str) -> bool:
        try:
            orders = self._exch.fetch_open_orders(symbol)
            for o in orders:
                if o.get("type") == "STOP_MARKET" and o.get("side") == "SELL":
                    self._exch.cancel_order(id=o["id"], symbol=symbol)
            return True
        except Exception:
            return False
    def cancel_all_take_profits(self, symbol: str) -> bool:
        try:
            orders = self._exch.fetch_open_orders(symbol)
            for o in orders:
                if o.get("type") == "TAKE_PROFIT_MARKET" and o.get("side") == "SELL":
                    self._exch.cancel_order(id=o["id"], symbol=symbol)
            return True
        except Exception:
            return False


    def fetch_open_interest(self, symbol: str) -> float | None:
        try:
            oi = self._exch.fetch_open_interest(symbol)
            return float(oi.get("openInterestAmount", 0) or 0) if oi else None
        except Exception:
            pass
        return None
    def open_long_market(
        self,
        symbol: str,
        quantity: float,
        stop_loss_price: float,
        tp_levels: list[dict],
    ) -> dict[str, Any]:
        """市价开多，成交后自动挂止盈止损单。"""
        results: dict[str, Any] = {
            "entry": None, "stop_loss": None, "take_profits": [],
        }

        self.set_leverage(symbol, self._cfg.leverage)

        qty = float(self._exch.amount_to_precision(symbol, quantity))
        qty = self._cap_qty(symbol, qty)
        if qty <= 0:
            err_msg = f"数量 {quantity} 经精度舍入后为 {qty}，无法开仓"
            logger.error(err_msg)
            results["error"] = err_msg
            return results

        logger.info("正在市价开多 %s 数量=%s", symbol, qty)

        try:
            entry_order = self._exch.create_order(symbol=symbol, type='MARKET', side='BUY', amount=qty, params={'positionSide': 'LONG'})
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
        self._挂止盈止损(symbol, qty, stop_loss_price, tp_levels, results)

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
            order = self._exch.create_order(symbol=symbol, type='LIMIT', side='BUY', amount=qty, price=limit_price_rounded, params={'positionSide': 'LONG'})
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
        results: dict[str, Any] = {"stop_loss": None, "take_profits": []}
        qty = float(self._exch.amount_to_precision(symbol, quantity))

        tp_levels = [{"price": take_profit_price, "qty_pct": 1.0, "label": "TP"}]
        self._挂止盈止损(symbol, qty, stop_loss_price, tp_levels, results)
        return results

# ── 全额平仓（清盘） ─────────────────────────────────────────────────

    def close_position_full(self, symbol: str) -> dict[str, Any]:
        """全额平仓——实时获取持仓量，用 reduceOnly 一键清零。"""
        result: dict[str, Any] = {
            "success": False,
            "order": None,
            "position_amt_before": 0.0,
            "error": None,
        }

        try:
            self._确保市场已加载()

            # 1. 通过 fetch_positions 获取实时持仓
            positions = self._exch.fetch_positions([symbol])
            pos_amt = 0.0
            pos_side = ""

            for pos in positions:
                amt = float(pos.get("contracts", 0) or pos.get("positionAmt", 0) or 0)
                if abs(amt) > 0:
                    pos_amt = abs(amt)
                    pos_side = pos.get("positionSide", "")
                    break

            result["position_amt_before"] = pos_amt

            if pos_amt <= 0:
                logger.info("close_position_full: %s 无持仓，跳过", symbol)
                result["success"] = True
                return result

            # 2. 决定买卖方向
            side = "SELL" if pos_side == "LONG" else "BUY"

            # 3. 精度处理
            qty = float(self._exch.amount_to_precision(symbol, pos_amt))

            # 4. 市价下单 + reduceOnly 安全锁
            logger.info(
                "全额平仓 %s: 方向=%s 数量=%s positionSide=%s",
                symbol, side, qty, pos_side,
            )

            order = self._exch.create_order(
                symbol=symbol,
                type="MARKET",
                side=side,
                amount=qty,
                params={
                    "reduceOnly": True,
                    "positionSide": pos_side,
                },
            )

            result["order"] = order
            result["success"] = True
            logger.info(
                "全额平仓成功 %s: order_id=%s filled=%s",
                symbol, order.get("id", "N/A"), order.get("filled", "N/A"),
            )

        except Exception as exc:
            logger.error("全额平仓失败 %s: %s", symbol, exc)
            result["error"] = str(exc)

        return result

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
        tp_levels: list[dict],
        results: dict,
    ) -> None:
        """挂止盈止损单，tp_levels 支持分批止盈。

        tp_levels: [{"price": ..., "qty_pct": 0.5}, ...]
        """
        # 止损
        if sl_price > 0:
            try:
                sl_rounded = float(self._exch.price_to_precision(symbol, sl_price))
                sl_order = self._exch.create_order(
                    symbol=symbol,
                    type="STOP_MARKET",
                    side="SELL",
                    amount=qty,
                    params={"stopPrice": sl_rounded, "positionSide": "LONG"},
                )
                results["stop_loss"] = sl_order
                logger.info(">>> 止损挂单成功: 价格=%s 订单号=%s",
                            sl_rounded, sl_order.get("id", "N/A"))
            except Exception as exc:
                logger.error("止损挂单失败 %s: %s", symbol, exc)

        # 分批止盈（支持多个 TP 价格，每个挂部分仓位）
        if tp_levels:
            results["take_profits"] = []
            for level in tp_levels:
                tp_price = level["price"]
                tp_qty_pct = level["qty_pct"]
                tp_label = level.get("label", "TP")
                if tp_price <= 0 or tp_qty_pct <= 0:
                    continue
                tp_qty = qty * tp_qty_pct
                tp_qty = float(self._exch.amount_to_precision(symbol, tp_qty))
                if tp_qty <= 0:
                    logger.warning("  %s 舍入后数量为0，跳过", tp_label)
                    continue
                try:
                    tp_rounded = float(self._exch.price_to_precision(symbol, tp_price))
                    tp_order = self._exch.create_order(
                        symbol=symbol,
                        type="TAKE_PROFIT_MARKET",
                        side="SELL",
                        amount=tp_qty,
                        params={"stopPrice": tp_rounded, "positionSide": "LONG"},
                    )
                    results["take_profits"].append(tp_order)
                    logger.info(">>> %s 止盈挂单成功: 价格=%s 数量=%s(%d%%) 订单号=%s",
                                tp_label, tp_rounded, tp_qty,
                                int(tp_qty_pct * 100),
                                tp_order.get("id", "N/A"))
                except Exception as exc:
                    logger.error("%s 止盈挂单失败 %s (price=%s): %s",
                                tp_label, symbol, tp_price, exc)

    def _构建交易所(self):
        exch = ccxt.binanceusdm({
            "apiKey": self._cfg.binance_api_key,
            "secret": self._cfg.binance_secret_key,
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,
            },
        })
        if self._cfg.binance_testnet:
            exch.enable_demo_trading(True)
            logger.warning("Binance 合约模拟模式已启用 —— 订单为模拟执行")
        exch.session = requests.Session()
        exch.session.trust_env = True
        exch.enableRateLimit = True
        return exch

    def _确保市场已加载(self) -> None:
        if self._markets_loaded:
            return
        logger.info("正在从 Binance 合约加载市场数据 ...")
        try:
            self._exch.load_markets()
        except Exception as exc:
            logger.error("加载市场数据失败: %s", exc)
            raise
        # 验证 markets 是否真正加载
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            if sym not in self._exch.markets:
                logger.warning("市场数据中未找到参考合约 %s", sym)
        self._markets_loaded = True
        logger.info("市场数据加载完成，共 %d 个", len(self._exch.markets))
