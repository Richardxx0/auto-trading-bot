"""
K线数据分析与技术指标计算。

使用交易所 OHLCV 数据计算 EMA、RSI、ATR 等指标，
评估当前价格位置并生成入场建议。
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """技术分析器 —— 获取 K 线 → 计算指标 → 判断入场条件。"""

    def __init__(self, exchange_client):
        self._exch = exchange_client

    def analyze(self, symbol: str, config) -> dict[str, Any]:
        """对 *symbol* 执行完整技术分析。

        步骤：
          1. 获取 4h 和 1h K 线数据
          2. 计算 EMA(20) / EMA(50) / RSI(14) / ATR(14)
          3. 判断趋势方向（上升 / 下降 / 震荡）
          4. 判断价格相对均线位置
          5. 给出入场评级（good / ok / poor）
          6. 若为 poor 则计算建议限价挂单价

        返回字典包含所有中间值和结论。
        """
        result: dict[str, Any] = {
            "symbol": symbol,
            "trend": "unknown",
            "rsi": None,
            "ema20": None,
            "ema50": None,
            "atr_pct": None,
            "price_vs_ema20_pct": None,
            "entry_zone": "unknown",
            "limit_price": None,
            "current_price": None,
            "error": None,
        }

        # --- 1. 获取 K 线数据 ---
        ohlcv_4h = self._exch.fetch_ohlcv(symbol, timeframe="4h", limit=100)
        ohlcv_1h = self._exch.fetch_ohlcv(symbol, timeframe="1h", limit=100)

        if not ohlcv_4h or len(ohlcv_4h) < 50:
            logger.warning("%s 4h K线数据不足（%d 条），跳过技术分析", symbol, len(ohlcv_4h or []))
            result["error"] = "K线数据不足"
            return result

        # --- 2. 提取收盘价和 OHLC ---
        close_4h = [c[4] for c in ohlcv_4h]
        close_1h = [c[4] for c in ohlcv_1h]
        high_4h  = [c[2] for c in ohlcv_4h]
        low_4h   = [c[3] for c in ohlcv_4h]
        volume_4h = [c[5] for c in ohlcv_4h]

        current_price = close_4h[-1]
        result["current_price"] = current_price

        # --- 3. 计算 EMA ---
        ema20 = self._ema(close_4h, config.analysis_ema_fast)
        ema50 = self._ema(close_4h, config.analysis_ema_slow)
        ema20_val = ema20[-1]
        ema50_val = ema50[-1]
        result["ema20"] = round(ema20_val, 10)
        result["ema50"] = round(ema50_val, 10)

        # --- 4. 计算 RSI(14) ---
        rsi = self._rsi(close_1h, config.analysis_rsi_period)
        rsi_val = rsi[-1]
        result["rsi"] = round(rsi_val, 2)

        # --- 5. 计算 ATR(14) ---
        atr = self._atr(high_4h, low_4h, close_4h, config.analysis_rsi_period)
        atr_val = atr[-1]
        atr_pct = atr_val / current_price if current_price > 0 else 0
        result["atr_pct"] = round(atr_pct, 6)

        # --- Volume MA20 ---
        vol_ma20 = sum(volume_4h[-20:]) / 20 if len(volume_4h) >= 20 else 0
        vol_ratio = volume_4h[-1] / vol_ma20 if vol_ma20 > 0 else 0
        result["vol_ratio"] = round(vol_ratio, 2)

        # --- 6. 判断趋势 ---
        if ema20_val > ema50_val * 1.005:
            trend = "up"
        elif ema20_val < ema50_val * 0.995:
            trend = "down"
        else:
            trend = "sideways"
        result["trend"] = trend

        # --- 7. 价格相对 EMA(20) 位置 ---
        price_vs_ema20_pct = (current_price / ema20_val - 1) * 100
        result["price_vs_ema20_pct"] = round(price_vs_ema20_pct, 4)

        # --- 8. 入场评级 ---
        entry_zone = self._评估入场(trend, rsi_val, price_vs_ema20_pct, config)
        result["entry_zone"] = entry_zone

        # --- 9. 若评级为 poor，计算建议限价位 ---
        if entry_zone == "poor":
            # 限价目标：EMA(20) 下方 0.5% 或 当前价减去 0.6 倍 ATR，取较低者
            limit_by_ema = ema20_val * 0.995
            limit_by_atr = current_price * (1 - atr_pct * 0.6)
            limit_price = min(limit_by_ema, limit_by_atr)
            # 保证限价不低于当前价的 90%
            limit_price = max(limit_price, current_price * 0.90)
            result["limit_price"] = round(limit_price, 10)

        # --- 10. 动态止损止盈（基于 ATR） ---
        if atr_pct > 0:
            sl_mult = 1.5
            result["sl_price"] = round(current_price * (1 - atr_pct * sl_mult), 10)
            result["sl_pct"] = round(atr_pct * sl_mult * 100, 4)
 
             # 分批止盈：三个阶梯
            result["tp_levels"] = [
                 {"price": round(current_price * (1 + atr_pct * 2.0), 10),
                 "qty_pct": 0.5, "label": "TP1"},
                 {"price": round(current_price * (1 + atr_pct * 3.0), 10),
                 "qty_pct": 0.3, "label": "TP2"},
                 {"price": round(current_price * (1 + atr_pct * 4.5), 10),
                 "qty_pct": 0.2, "label": "TP3"},
             ]
        else:
            result["sl_price"] = None
            result["tp_levels"] = []
 
        logger.info(
            "技术分析完成: %s 趋势=%s RSI=%.1f EMA20=%.8f "
            "price_vs_EMA20=%.2f%% atr=%.4f%% 入场评级=%s 限价=%.8f "
            "动态SL=%s TP1=%s TP2=%s TP3=%s",
            symbol, trend, rsi_val, ema20_val,
            price_vs_ema20_pct, atr_pct * 100, entry_zone,
            result.get("limit_price") or 0,
            result.get("sl_price") or "N/A",
            result["tp_levels"][0]["price"] if result.get("tp_levels") else "N/A",
            result["tp_levels"][1]["price"] if len(result.get("tp_levels", [])) > 1 else "N/A",
            result["tp_levels"][2]["price"] if len(result.get("tp_levels", [])) > 2 else "N/A",
        )

        return result

    # ── 指标计算方法 ─────────────────────────────────────

    @staticmethod
    def _ema(prices: list[float], period: int) -> list[float]:
        """指数移动平均（纯 Python 实现）。返回与输入等长的列表，
        前 ``period-1`` 个位置填充 ``None`` 占位。"""
        result: list[float] = []
        if len(prices) < period:
            return result

        # 初始 SMA
        sma = sum(prices[:period]) / period
        result.extend([None] * (period - 1))
        result.append(sma)

        multiplier = 2.0 / (period + 1)
        for p in prices[period:]:
            ema = (p - result[-1]) * multiplier + result[-1]
            result.append(ema)

        return result

    @staticmethod
    def _rsi(prices: list[float], period: int = 14) -> list[float]:
        """相对强弱指数。返回与输入等长的列表，前 ``period`` 个位置
        填充 ``None`` 占位。"""
        if len(prices) < period + 1:
            return [None] * len(prices)

        result: list[float] = [None] * period

        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, period + 1):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - 100.0 / (1.0 + rs))

        for i in range(period + 1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gain = max(diff, 0)
            loss = max(-diff, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

            if avg_loss == 0:
                result.append(100.0)
            else:
                rs = avg_gain / avg_loss
                result.append(100.0 - 100.0 / (1.0 + rs))

        return result

    @staticmethod
    def _atr(
        high: list[float],
        low: list[float],
        close: list[float],
        period: int = 14,
    ) -> list[float]:
        """平均真实波幅。返回与输入等长的列表，前 ``1`` 个位置
        填充 ``None`` 占位。"""
        if len(high) < period + 1:
            return [None] * len(high)

        result: list[float] = [None]
        true_ranges: list[float] = []

        for i in range(1, len(high)):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
            true_ranges.append(tr)

        # 初始 SMA of TR
        initial_atr = sum(true_ranges[:period]) / period
        result.append(initial_atr)

        for i in range(period, len(true_ranges)):
            atr_val = (result[-1] * (period - 1) + true_ranges[i]) / period
            result.append(atr_val)

        # 补齐末尾使得长度与 close 一致
        while len(result) < len(close):
            result.append(result[-1])

        return result

    # ── 入场判断 ─────────────────────────────────────────

    @staticmethod
    def _评估入场(
        trend: str,
        rsi: float,
        price_vs_ema20_pct: float,
        config,
    ) -> str:
        """根据趋势、RSI、价格位置给出入场评级。

        返回 ``"good"`` / ``"ok"`` / ``"poor"``。
        """
        os_threshold = config.analysis_rsi_oversold   # 超卖阈值
        ob_threshold = config.analysis_rsi_overbought  # 超买阈值

        # 价格偏离 EMA(20) 容忍度
        max_deviation = config.analysis_max_deviation_pct

        if trend == "up":
            if rsi < os_threshold:
                return "good"       # 上升趋势 + 超卖 → 极好入场
            elif rsi <= ob_threshold and abs(price_vs_ema20_pct) <= max_deviation:
                return "good"       # 上升趋势 + 价格在均线附近 → 好入场
            elif rsi <= ob_threshold:
                return "ok"         # 上升趋势 + 价格略偏离 → 可入场
            else:
                return "poor"       # 上升趋势但 RSI 偏高 → 等回调

        elif trend == "sideways":
            if rsi < os_threshold:
                return "good"
            elif rsi < 45:
                return "ok"
            else:
                return "poor"
        else:
            # 下降趋势 —— 不建议做多
            return "poor"

    # ── 市场状态分类 ──────────────────────────────────────

    def classify_regime(self, symbol: str, config) -> dict:
        """市场状态分类。

        输入 4h K 线 → 输出三种状态：
          - TRENDING（趋势）：ADX > 阈值 或 EMA20/50 明显发散
          - RANGING（震荡）：EMA20/50 缠绕，ATR 偏低
          - VOLATILE（高波动）：ATR 显著偏高 或 价格严重偏离均线

        返回字典：
          {
            "regime": "TRENDING" | "RANGING" | "VOLATILE",
            "strength": "HIGH" | "MID" | "LOW",
            "adx": float,
            "ema_spread_pct": float,
            "atr_ratio": float,
            "ema_slope_pct": float,
            "price_dev_pct": float,
          }
        """
        ohlcv = self._exch.fetch_ohlcv(symbol, timeframe="4h", limit=100)

        result: dict = {
            "regime": "RANGING",
            "strength": "LOW",
            "adx": None,
            "ema_spread_pct": None,
            "atr_ratio": None,
            "ema_slope_pct": None,
            "price_dev_pct": None,
        }

        if not ohlcv or len(ohlcv) < 50:
            logger.warning("%s 4h K线不足50条，无法分类市场状态", symbol)
            return result

        close = [float(c[4]) for c in ohlcv]
        high  = [float(c[2]) for c in ohlcv]
        low   = [float(c[3]) for c in ohlcv]

        # --- 计算各项指标 ---
        ema20_raw = self._ema(close, 20)
        ema50_raw = self._ema(close, 50)
        atr_raw   = self._atr(high, low, close, 14)
        adx_raw   = self._adx(high, low, close, 14)

        # 去掉前导 None
        ema20 = [x for x in ema20_raw if x is not None]
        ema50 = [x for x in ema50_raw if x is not None]
        atr   = [x for x in atr_raw if x is not None]
        adx   = [x for x in adx_raw if x is not None]

        if not ema20 or not ema50 or not atr or not adx:
            logger.warning("%s 指标数据不足，无法分类市场状态", symbol)
            return result

        ema20_v = ema20[-1]
        ema50_v = ema50[-1]
        atr_v   = atr[-1]
        adx_v   = adx[-1]
        price   = close[-1]
        threshold = config.analysis_adx_threshold
        volatile_ratio = config.analysis_volatile_atr_ratio

        # --- EMA 发散度 ---
        ema_spread = abs(ema20_v - ema50_v) / max(ema50_v, 1e-10) * 100

        # --- ATR 比值（当前 / 历史中位数）---
        atr_sorted = sorted(atr)
        atr_median = atr_sorted[len(atr_sorted) // 2]
        atr_ratio  = atr_v / atr_median if atr_median > 0 else 1.0

        # --- EMA 斜率（近5根）---
        if len(ema20) >= 5:
            ema_slope = (ema20[-1] - ema20[-5]) / max(abs(ema20[-5]), 1e-10) * 100
        else:
            ema_slope = 0.0

        # --- 价格偏离 EMA20 程度 ---
        price_dev = abs(price - ema20_v) / max(ema20_v, 1e-10) * 100

        # ── 分类判断 ──────────────────────────────────────
        # 优先级：VOLATILE > TRENDING > RANGING

        # VOLATILE：ATR 比值超标 或 价格严重偏离
        if atr_ratio > volatile_ratio or price_dev > config.analysis_max_deviation_pct * 2:
            regime = "VOLATILE"
            strength = "HIGH" if atr_ratio > volatile_ratio * 1.3 else "MID"

        # TRENDING：ADX 达标 或 EMA 明显发散 + 有斜率
        elif adx_v >= threshold or (ema_spread > 2.0 and abs(ema_slope) > 0.05):
            regime = "TRENDING"
            if adx_v >= threshold + 10 or abs(ema_slope) > 0.2:
                strength = "HIGH"
            elif adx_v >= threshold:
                strength = "MID"
            else:
                strength = "LOW"

        # RANGING：其他情况
        else:
            regime = "RANGING"
            strength = "HIGH" if atr_ratio < 0.7 else "MID"

        result = {
            "regime": regime,
            "strength": strength,
            "adx": round(adx_v, 2),
            "ema_spread_pct": round(ema_spread, 4),
            "atr_ratio": round(atr_ratio, 4),
            "ema_slope_pct": round(ema_slope, 4),
            "price_dev_pct": round(price_dev, 4),
        }

        logger.info(
            "市场状态分类: %s 强度=%s ADX=%.1f "
            "EMA发散=%.2f%% ATR比值=%.2f EMA斜率=%.4f%% 偏离度=%.2f%%",
            regime, strength, adx_v,
            ema_spread, atr_ratio, ema_slope, price_dev,
        )

        return result

    # ── ADX 计算 ─────────────────────────────────────────────

    @staticmethod
    def _adx(
        high: list[float],
        low: list[float],
        close: list[float],
        period: int = 14,
    ) -> list[float | None]:
        """计算平均趋向指数 ADX。

        返回列表长度与 *high* 一致，前 ``period * 2`` 个位置为 None。
        """
        n = len(high)
        if n < period * 2:
            return [None] * n

        tr_list:       list[float] = []
        plus_dm_list:  list[float] = []
        minus_dm_list: list[float] = []

        for i in range(1, n):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
            tr_list.append(tr)

            up_move   = high[i] - high[i - 1]
            down_move = low[i - 1] - low[i]

            plus_dm  = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        # Wilder 平滑
        smooth_tr   = TechnicalAnalyzer._wilder_smooth(tr_list, period)
        smooth_pdm  = TechnicalAnalyzer._wilder_smooth(plus_dm_list, period)
        smooth_mdm  = TechnicalAnalyzer._wilder_smooth(minus_dm_list, period)

        pdi = [100.0 * p / t if t > 0 else 0.0
               for p, t in zip(smooth_pdm, smooth_tr)]
        mdi = [100.0 * m / t if t > 0 else 0.0
               for m, t in zip(smooth_mdm, smooth_tr)]

        dx = [100.0 * abs(p - m) / (p + m) if (p + m) > 0 else 0.0
              for p, m in zip(pdi, mdi)]

        adx_vals = TechnicalAnalyzer._wilder_smooth(dx, period)

        # 填充前导 None 使长度与 high 一致
        pad = n - len(adx_vals)
        return [None] * pad + adx_vals

    @staticmethod
    def _wilder_smooth(
        values: list[float],
        period: int,
    ) -> list[float]:
        """Wilder 平滑（初始 SMA，后续递归：(prev × (n-1) + current) / n）。"""
        if len(values) < period:
            return []

        result: list[float] = []
        sma = sum(values[:period]) / period
        result.append(sma)

        for v in values[period:]:
            smoothed = (result[-1] * (period - 1) + v) / period
            result.append(smoothed)

        return result
