"""
解析 Telegram YSS 信号消息。

解析策略（两阶段）：
  阶段 A — 结构化字段提取（半结构化列表格式）
  阶段 B — 行内关键词兜底

支持的消息格式：

  [阶段 A] 列表结构：
    • 品种：CLO
    • 当前价格：0.1499
    • 目前所属：中流动性区

  [阶段 B] 行内关键词：
    首次买入信号——CLO        或    CLO 首次买入信号
    【第一次买入信号】CLO

消息中必须包含触发关键词"首次买入信号"。
"""
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SignalParser:
    """解析 Telegram 消息中的"首次买入信号"，通过正则提取结构化字段。

    输出字典（为未来扩展预留字段）：
      {
        "raw_message": str,         # 原始 Telegram 文本
        "symbol":       str,         # 币种，如 "CLO"
        "price":        float|None,  # 价格，如 0.1499 ｜ 缺失时为 None
        "signal_type":  str,         # 信号类型，始终为 "LONG"
        # 预留字段（未来可扩展）：
        # "leverage":   int|None,
        # "tp":         float|None,
        # "sl":         float|None,
      }
    """

    TRIGGER_KEYWORD = "首次买入信号"

    # ── 阶段 A：结构化字段正则 ───────────────────────────────

    # 支持的 Symbol 标签：品种 / 交易对 / 币种 / symbol / Symbol
    _SYMBOL_RE = re.compile(
        r"(?:品种|交易对|币种|symbol|Symbol|币)\s*[:：]\s*([A-Za-z0-9]{2,20})",
    )

    # 支持的价格标签：价格 / 当前价格 / 入场价 / 入场 / 现价 / entry / Entry / price / Price
    _PRICE_RE = re.compile(
        r"(?:(?:当前)?价格|入场价?|进场价?|现价|[Ee]ntry|[Pp]rice)\s*[:：]\s*([0-9]+\.?[0-9]*)",
    )

    # （预留字段正则模板 — 杠杆、止盈、止损等）
    # _LEVERAGE_RE = re.compile(...)
    # _TP_RE        = re.compile(...)
    # _SL_RE        = re.compile(...)

    # ── 阶段 B：行内关键词正则（兜底） ──────────────────────

    # "首次买入信号——CLO"  或  "CLO 首次买入信号"
    _INLINE_SYMBOL_RE = re.compile(
        r"""
        (?:
            首次买入信号[\s\-–—]*
            |
            [\s\-–—]*首次买入信号
            |
            [【\[]第一次?买入信号[】\]][\s\-–—]*
            |
            [\s\-–—]*[【\[]第一次?买入信号[】\]]
        )
        (?P<coin>[A-Za-z0-9]{2,20})
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _INLINE_FALLBACK_RE = re.compile(
        r"首次买入信号[\s\-–—]*([A-Z0-9]{2,10})",
        re.IGNORECASE,
    )

    # 行内价格正则
    _INLINE_PRICE_RE = re.compile(
        r"(?:价格?|入场|现价|[Ee]ntry)\s*[:：]?\s*([0-9]+\.?[0-9]*)",
    )

    # ── 公开方法 ─────────────────────────────────────────────

    def parse(self, text: str) -> dict[str, Any] | None:
        """解析 *text* 并返回信号字典。

        返回值：
          - 合法信号 → dict
          - 非法信号 → None
        """
        if not text:
            return None

        # 必须包含触发关键词
        if self.TRIGGER_KEYWORD not in text:
            return None

        # 始终记录原始消息，便于排查
        logger.info("[原始信号] %s", text.strip())

        # ── 阶段 A：结构化字段提取 ───────────────────────────
        symbol = self._提取结构化币种(text)
        price  = self._提取结构化价格(text)

        # ── 阶段 B：行内兜底（阶段 A 未找到币种时触发） ──────────
        if symbol is None:
            symbol = self._提取行内币种(text)
            if price is None:
                price = self._提取行内价格(text)

        # 币种仍缺失 → 丢弃信号
        if symbol is None:
            logger.warning(
                "消息包含关键词但未能提取到币种。text=%s",
                text[:120],
            )
            return None

        # 价格缺失 → 警告，但不致命（之后会用实时市价）
        if price is None:
            logger.warning(
                "信号 %s 解析成功但未包含价格，将使用实时市价。text=%s",
                symbol, text[:80],
            )

        result: dict[str, Any] = {
            "raw_message": text,
            "symbol":      symbol,
            "price":       price,
            "signal_type": "LONG",
        }

        logger.info("[解析结果] %s", result)
        return result

    # ── 阶段 A 提取器 ────────────────────────────────────────

    @staticmethod
    def _提取结构化币种(text: str) -> str | None:
        """在消息中查找如 ``品种：CLO`` 的字段并返回值。"""
        m = SignalParser._SYMBOL_RE.search(text)
        if m:
            return m.group(1).upper()
        return None

    @staticmethod
    def _提取结构化价格(text: str) -> float | None:
        """在消息中查找如 ``当前价格：0.1499`` 的字段并返回值。"""
        m = SignalParser._PRICE_RE.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    # ── 阶段 B 提取器 ────────────────────────────────────────

    @staticmethod
    def _提取行内币种(text: str) -> str | None:
        """在行内关键词中提取币种，如 ``首次买入信号——CLO``。"""
        m = SignalParser._INLINE_SYMBOL_RE.search(text)
        if m:
            return m.group("coin").upper()
        m = SignalParser._INLINE_FALLBACK_RE.search(text)
        if m:
            return m.group(1).upper()
        return None

    @staticmethod
    def _提取行内价格(text: str) -> float | None:
        """在行内关键词附近提取价格。"""
        m = SignalParser._INLINE_PRICE_RE.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None
