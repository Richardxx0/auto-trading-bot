"""
仓位计算与止盈止损定价。

风险模型：固定比例风险（账户余额的 2%）。
止损：入场价下方 2%。
止盈：入场价上方 4%（风险收益比 1:2）。
"""
import logging

from config.settings import Config

logger = logging.getLogger(__name__)


class RiskManager:
    """计算开仓数量、止损价和止盈价。"""

    def __init__(self, config: Config):
        self._cfg = config
        self._max_position_pct = getattr(config, 'max_position_pct', 0.10)
        self._max_margin_pct = getattr(config, 'max_margin_pct', 0.10)

    def calculate(
        self,
        balance_usdt: float,
        entry_price: float,
        direction: str = "BUY",
    ) -> dict:
        """计算仓位参数。

        返回 ``{"qty", "sl", "tp"}`` 字典：
          ``qty`` — 合约数量（原始值，由 exchange 层按精度舍入）
          ``sl``  — 止损价格
          ``tp``  — 止盈价格
        """
        # 固定风险金额
        risk_amount = balance_usdt * self._cfg.risk_per_trade

        # 根据方向计算止损价和止盈价
        if direction.upper() == "BUY":
            sl_price = entry_price * (1.0 - self._cfg.stop_loss_pct)
            tp_price = entry_price * (1.0 + self._cfg.take_profit_pct)
        else:
            sl_price = entry_price * (1.0 + self._cfg.stop_loss_pct)
            tp_price = entry_price * (1.0 - self._cfg.take_profit_pct)

        sl_distance = abs(entry_price - sl_price)

        if sl_distance <= 0 or risk_amount <= 0:
            logger.warning(
                "无效输入: balance=%.2f entry=%.8f",
                balance_usdt, entry_price,
            )
            return {"qty": 0.0, "sl": 0.0, "tp": 0.0}

        # 原始数量（交易所精度舍入由 exchange 层负责）
        raw_qty = risk_amount / sl_distance

        # ── 第一道锁：单仓最多占余额 max_position_pct ──
        max_position_value = balance_usdt * self._max_position_pct
        max_qty_by_position = max_position_value / entry_price

        # ── 第二道锁：保证金上限 ──
        max_margin = balance_usdt * self._max_margin_pct
        max_qty_by_margin = max_margin * self._cfg.leverage / entry_price

        # 取三者最小值
        qty = min(raw_qty, max_qty_by_position, max_qty_by_margin)

        if qty < raw_qty:
            logger.info(
                '  仓位截断: risk_qty=%.4f → position_cap=%.4f → margin_cap=%.4f → final=%.4f '
                '(资金$%.2f=%.1f%%  保证金$%.2f=%.1f%%)',
                raw_qty, max_qty_by_position, max_qty_by_margin, qty,
                max_position_value, self._max_position_pct * 100,
                max_margin, self._max_margin_pct * 100,
            )

        # ── 第三道安全锁：硬上限 15%，防止配置错误满仓 ──
        position_value = qty * entry_price
        safety_limit = balance_usdt * 0.15
        if position_value > safety_limit:
            logger.error(
                "【安全熔断】position=%.2f USDT 超出上限 %.2f USDT (15%% of balance)，拒绝下单",
                position_value, safety_limit,
            )
            return {"qty": 0.0, "sl": 0.0, "tp": 0.0}

        result = {
            "qty": qty,
            "sl": sl_price,
            "tp": tp_price,
        }

        logger.info(
            "风险计算: 余额=%.2f 风险额=%.4f "
            "入场=%.8f 止损=%.8f 止盈=%.8f 最终数量=%.6f "
            "仓位价值=%.2f USDT (占比 %.1f%%)",
            balance_usdt, risk_amount,
            entry_price, sl_price, tp_price, qty,
            position_value, position_value / balance_usdt * 100 if balance_usdt > 0 else 0,
        )

        return result
