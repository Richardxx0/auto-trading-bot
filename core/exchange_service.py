"""ExchangeService —— 异步包装同步 CCXT，支持超时 + 重试 + 串行锁。

用法：
    service = ExchangeService(exchange_client)
    balance = await service.get_balance_usdt()
    price = await service.get_current_price("BTCUSDT")
"""
import asyncio
import logging
from typing import Any

# ExchangeClient passed via constructor

logger = logging.getLogger(__name__)


class ExchangeService:
    """异步包装 ExchangeClient，所有 ccxt 调用走线程池 + 超时 + 重试 + 锁。"""

    # 各接口超时配置（秒）
    TIMEOUTS = {
        "balance": 8,
        "position": 8,
        "ticker": 8,
        "ohlcv": 10,
        "order": 10,
        "cancel": 8,
        "leverage": 8,
        "contract": 8,
        "precision": 8,
    }

    def __init__(self, exchange):  # ExchangeClient
        self._exch = exchange
        self._lock = asyncio.Lock()

    async def run(self, func, *args, timeout: int = 8) -> Any:
        """在独立线程中运行同步函数，带超时 + 重试 + 串行锁。

        流程：
            1. 获取锁（防并发）
            2. 最多重试 3 次（间隔 0.5s → 1s）
            3. asyncio.to_thread + asyncio.wait_for
        """
        last_exc = None
        for attempt in range(3):
            try:
                async with self._lock:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(func, *args),
                        timeout=timeout,
                    )
                return result
            except asyncio.TimeoutError:
                logger.warning(
                    "[EXCH] %s 超时(%ds) 第%d次",
                    func.__name__, timeout, attempt + 1,
                )
                last_exc = asyncio.TimeoutError(f"超时 {timeout}s")
            except Exception as exc:
                logger.warning(
                    "[EXCH] %s 失败(%s) 第%d次",
                    func.__name__, exc, attempt + 1,
                )
                last_exc = exc

            if attempt < 2:
                await asyncio.sleep(0.5 + attempt * 0.5)  # 0.5s → 1s

        raise last_exc  # type: ignore[misc]

    # ── 账户 & 合约查询 ──────────────────────────────────────

    async def contract_exists(self, symbol: str) -> bool:
        try:
            return await self.run(
                self._exch.contract_exists, symbol,
                timeout=self.TIMEOUTS["contract"],
            )
        except Exception:
            return False

    async def get_current_price(self, symbol: str) -> float | None:
        try:
            return await self.run(
                self._exch.get_current_price, symbol,
                timeout=self.TIMEOUTS["ticker"],
            )
        except Exception:
            return None

    async def get_balance_usdt(self) -> float:
        try:
            return await self.run(
                self._exch.get_balance_usdt,
                timeout=self.TIMEOUTS["balance"],
            )
        except Exception:
            return 0.0

    async def query_position(self, symbol: str) -> dict | None:
        try:
            return await self.run(
                self._exch.query_position, symbol,
                timeout=self.TIMEOUTS["position"],
            )
        except Exception:
            return None

    async def has_open_position(self, symbol: str) -> bool:
        return (await self.query_position(symbol)) is not None

    async def get_open_positions_count(self) -> int:
        try:
            return await self.run(
                self._exch.get_open_positions_count,
                timeout=self.TIMEOUTS["position"],
            )
        except Exception:
            return 0

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "4h", limit: int = 100,
    ) -> list[list[float]]:
        try:
            return await self.run(
                self._exch.fetch_ohlcv, symbol, timeframe, limit,
                timeout=self.TIMEOUTS["ohlcv"],
            )
        except Exception:
            return []

    async def fetch_open_interest(self, symbol: str) -> float | None:
        try:
            return await self.run(
                self._exch.fetch_open_interest, symbol,
                timeout=self.TIMEOUTS["ticker"],
            )
        except Exception:
            return None

    async def fetch_funding_rate(self, symbol: str) -> float:
        try:
            return await self.run(
                self._exch.fetch_funding_rate, symbol,
                timeout=self.TIMEOUTS["ticker"],
            )
        except Exception:
            return 0.0

    # ── 杠杆 ────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            await self.run(
                self._exch.set_leverage, symbol, leverage,
                timeout=self.TIMEOUTS["leverage"],
            )
        except Exception:
            pass

    # ── 开仓 ────────────────────────────────────────────────

    async def open_long_market(
        self,
        symbol: str,
        quantity: float,
        stop_loss_price: float,
        tp_levels: list[dict],
    ) -> dict[str, Any]:
        try:
            return await self.run(
                self._exch.open_long_market,
                symbol, quantity, stop_loss_price, tp_levels,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception as exc:
            logger.error("市价开多失败 %s: %s", symbol, exc)
            return {"error": str(exc)}

    async def open_long_limit(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
    ) -> dict[str, Any]:
        try:
            return await self.run(
                self._exch.open_long_limit,
                symbol, quantity, limit_price,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception as exc:
            logger.error("限价开多失败 %s: %s", symbol, exc)
            return {"error": str(exc)}

    # ── 订单管理 ────────────────────────────────────────────

    async def fetch_order_status(self, symbol: str, order_id: str) -> dict:
        try:
            return await self.run(
                self._exch.fetch_order_status, symbol, order_id,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception as exc:
            logger.error("查询订单 %s 状态失败: %s", order_id, exc)
            return {"status": "error", "info": str(exc)}

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            return await self.run(
                self._exch.cancel_order, symbol, order_id,
                timeout=self.TIMEOUTS["cancel"],
            )
        except Exception:
            return False

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        quantity: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict[str, Any]:
        try:
            return await self.run(
                self._exch.set_stop_loss_take_profit,
                symbol, quantity, stop_loss_price, take_profit_price,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception as exc:
            logger.error("设置止盈止损失败 %s: %s", symbol, exc)
            return {"error": str(exc)}

    # ── 全频平仓（清盘） ─────────────────────────────────────────────────

    async def close_position_full(self, symbol: str) -> dict:
        """全额平仓——实时获取持仓量，reduceOnly 一键清零。"""
        try:
            return await self.run(
                self._exch.close_position_full, symbol,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception as exc:
            logger.error("全额平仓失败 %s: %s", symbol, exc)
            return {"success": False, "error": str(exc)}

    # ── 止损管理 ────────────────────────────────────────────

    async def place_stop_loss_order(
        self, symbol: str, qty: float, sl_price: float,
    ) -> str | None:
        try:
            return await self.run(
                self._exch.place_stop_loss_order, symbol, qty, sl_price,
                timeout=self.TIMEOUTS["order"],
            )
        except Exception:
            return None

    async def cancel_all_stop_loss(self, symbol: str) -> bool:
        try:
            return await self.run(
                self._exch.cancel_all_stop_loss, symbol,
                timeout=self.TIMEOUTS["cancel"],
            )
        except Exception:
            return False

    # ── 精度工具 ────────────────────────────────────────────

    async def cancel_all_take_profits(self, symbol: str) -> bool:
        try:
            return await self.run(
                self._exch.cancel_all_take_profits, symbol,
                timeout=self.TIMEOUTS["cancel"],
            )
        except Exception:
            return False

    async def amount_to_precision(self, symbol: str, amount: float) -> float:
        """获取币种的数量精度。"""
        try:
            return float(
                await self.run(
                    self._exch._exch.amount_to_precision, symbol, amount,
                    timeout=self.TIMEOUTS["precision"],
                )
            )
        except Exception:
            return amount

    async def price_to_precision(self, symbol: str, price: float) -> float:
        """获取币种的价格精度。"""
        try:
            return float(
                await self.run(
                    self._exch._exch.price_to_precision, symbol, price,
                    timeout=self.TIMEOUTS["precision"],
                )
            )
        except Exception:
            return price
