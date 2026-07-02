"""Position Service — 统一持仓查询入口，标准化 Binance PositionRisk 返回格式。"""

import logging
from typing import Any
from dashboard import trade_store as ts
from dashboard.trade_store import normalize_symbol
from src.trade_constants import CloseReason

logger = logging.getLogger(__name__)

MIN_NOTIONAL = 1.0  # USDT，名义价值低于此值的仓位视为残仓，不参与交易决策


def _is_dust_position(pos: dict[str, Any]) -> bool:
    """判断指定持仓是否为无交易价值的残仓（dust position）。

    dust 特征：名义价值极小，通常来自部分平仓精度截断。
    """
    notional = abs(pos["position_amt"] * pos["mark_price"])
    return notional < MIN_NOTIONAL


def _normalize_position(raw: dict) -> dict[str, Any]:
    """将 Binance PositionRisk 原始数据标准化为统一 DTO。"""
    amt = float(raw.get("positionAmt", 0) or 0)
    side = raw.get("positionSide", "")
    if not side or side == "BOTH":
        side = "LONG" if amt > 0 else "SHORT"
    upnl_raw = raw.get("unRealizedProfit") or raw.get("unrealizedPnl", 0) or 0
    return {
        "symbol": raw.get("symbol", ""),
        "position_amt": amt,
        "entry_price": float(raw.get("entryPrice", 0) or 0),
        "mark_price": float(raw.get("markPrice", 0) or 0),
        "unrealized_pnl": float(upnl_raw),
        "side": side,
        "liquidation_price": float(raw.get("liquidationPrice", 0) or 0),
        "margin": float(raw.get("initialMargin") or raw.get("isolatedMargin", 0) or 0),
        "raw": raw,
    }


class PositionService:
    """统一持仓查询服务。

    所有调用方通过此服务获取持仓信息，不再直接访问 ExchangeClient._exch。
    """

    def __init__(self, exchange_client) -> None:
        self._exch = exchange_client

    # ── 查询接口 ────────────────────────────────────────────

    def get_open_positions(self) -> list[dict[str, Any]]:
        """获取所有持仓，返回统一 DTO 列表。"""
        try:
            raw_list = self._exch._exch.fapiPrivateV2GetPositionRisk()
            result = []
            for raw in raw_list:
                pos = _normalize_position(raw)
                if abs(pos["position_amt"]) > 0.001 and not _is_dust_position(pos):
                    result.append(pos)
            return result
        except Exception as exc:
            logger.warning("获取持仓失败: %s", exc)
            return []

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """查询指定合约的持仓，不存在返回 None。"""
        for pos in self.get_open_positions():
            if pos["symbol"] == symbol:
                return pos
        return None

    def has_open_position(self, symbol: str) -> bool:
        """判断指定合约是否有持仓。"""
        return self.get_position(symbol) is not None

    def get_open_positions_count(self) -> int:
        """获取持仓数量。"""
        return len(self.get_open_positions())

    # ── 余额查询 ────────────────────────────────────────────

    def get_account_info(self) -> dict[str, Any]:
        """获取合约账户信息。"""
        try:
            return self._exch._exch.fapiPrivateV2GetAccount()
        except Exception as exc:
            logger.warning("获取账户信息失败: %s", exc)
            return {}

    def get_balance_usdt(self) -> float:
        """获取 USDT 余额。"""
        try:
            acct = self.get_account_info()
            return float(acct.get("totalWalletBalance", 0))
        except Exception:
            return 0.0

    def get_asset_balances(self) -> list[dict[str, Any]]:
        """获取所有资产余额明细。"""
        try:
            bals = self._exch._exch.fapiPrivateV2GetBalance()
            result = []
            for b in bals:
                wb = float(b.get("walletBalance", 0))
                if wb > 0:
                    result.append({
                        "asset": b.get("asset", ""),
                        "wallet_balance": round(wb, 2),
                        "available_balance": round(float(b.get("availableBalance", 0)), 2),
                    })
            return result
        except Exception as exc:
            logger.warning("获取资产余额失败: %s", exc)
            return []

    # ── 状态同步 ────────────────────────────────────────────

    def sync_positions(self) -> None:
        """将本地 trade_store 持仓状态与交易所对齐。

        只做状态收敛（OPEN→CLOSED）。包含残仓清理。
        PnL 回填由 Income History 模块在 P2 处理。
        """
        try:
            positions = self.get_open_positions()
        except Exception as exc:
            logger.warning("sync_positions: 获取持仓失败: %s", exc)
            return

        # 交易所当前有效持仓 symbol 集合（已标准化，不含残仓）
        exchange_symbols = set()
        dust_symbols = set()
        for p in positions:
            sym = normalize_symbol(p["symbol"])
            if p.get("is_dust"):
                dust_symbols.add(sym)
            else:
                exchange_symbols.add(sym)

        # 处理残仓：关闭本地对应记录
        if dust_symbols:
            local_trades = ts.load_all()
            for t in local_trades:
                if t.get("status") != "OPEN":
                    continue
                t_sym = normalize_symbol(t.get("symbol", ""))
                if t_sym not in dust_symbols:
                    continue
                ts.close_trade(
                    t["id"],
                    realized_pnl=None,
                    close_price=None,
                    close_reason=CloseReason.SYNC_DUST,
                )
                from src import dedup_service
                dedup_service.set_status(t_sym, "CLOSED")
                logger.info("[SYNC] 清理残仓: %s %s | 原因=%s",
                            t.get("symbol"), t.get("direction", ""), CloseReason.SYNC_DUST)

        # 找出本地 OPEN 但在交易所不存在的仓位
        local_trades = ts.load_all()
        for t in local_trades:
            if t.get("status") != "OPEN":
                continue
            t_sym = normalize_symbol(t.get("symbol", ""))
            if t_sym in exchange_symbols:
                continue
            ts.close_trade(
                t["id"],
                realized_pnl=None,
                close_price=None,
                close_reason=CloseReason.SYNC,
            )
            from src import dedup_service
            dedup_service.set_status(t_sym, "CLOSED")
            logger.info("[SYNC] 关闭幽灵仓位: %s %s | 原因=%s",
                        t.get("symbol"), t.get("direction", ""), CloseReason.SYNC)
