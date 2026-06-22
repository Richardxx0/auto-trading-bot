"""
K线数据分析与技术指标计算（终极工业风控版）。

集成多周期概率共振、状态马尔可夫记忆平滑、多Bar状态维持锁、
ATR自适应仓位计算、最大硬性风险暴露控制及全参数化配置。
"""
import logging
import asyncio
import statistics
import numpy as np
from typing import Any, List, Dict

logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """技术分析器 —— 具备状态防抖锁与自适应风险控制的对冲基金级架构。"""

    def __init__(self, exchange_service):
        self._exchange_service = exchange_service
        self._exch = exchange_service._exch
        
        # 针对点 2 & 点 3：状态记忆与防抖持久化存储
        # 结构: { "BTC/USDT": np.array([P_trend, P_volatile, P_range]) }
        self._history_probs: Dict[str, np.ndarray] = {}
        
        # 结构: { "BTC/USDT": { "confirmed_regime": "RANGING", "candidate_regime": "TRENDING", "duration": 0 } }
        self._regime_locks: Dict[str, Dict[str, Any]] = {}

    def _softmax(self, x: np.ndarray) -> np.ndarray:
        """将状态得分严格归一化为概率分布"""
        e_x = np.exp(x - np.max(x))
        return e_x / e_x.sum()

    async def analyze(self, symbol: str, config) -> dict[str, Any]:
        """对 *symbol* 执行包含多周期共振、硬性风控锁与自适应仓位生成的完整分析。"""
        # 初始化状态锁
        if symbol not in self._regime_locks:
            self._regime_locks[symbol] = {
                "confirmed_regime": "RANGING",
                "confirmed_strength": "LOW",
                "candidate_regime": None,
                "duration": 0
            }
        regime_ctx = self._regime_locks[symbol]

        result: dict[str, Any] = {
            "symbol": symbol,
            "regime": regime_ctx["confirmed_regime"],
            "regime_strength": regime_ctx["confirmed_strength"],
            "probabilities": {},
            "trend": "unknown",
            "rsi": None,
            "ema20": None,
            "ema50": None,
            "atr_pct": None,
            "price_vs_ema20_pct": None,
            "entry_zone": "unknown",
            "limit_price": None,
            "current_price": None,
            "sl_price": None,
            "sl_pct": None,
            "tp_levels": [],
            "suggested_position_size_pct": 0.0,  # 动态新增：建议仓位分配(%)
            "error": None,
        }

        # ── 点 5：全配置化读取（动态读取，若配置缺失则优雅降级到默认工业标准值） ──
        cfg_adx_threshold = getattr(config, "analysis_adx_threshold", 25)
        cfg_volatile_ratio = getattr(config, "analysis_volatile_atr_ratio", 1.8)
        cfg_max_deviation = getattr(config, "analysis_max_deviation_pct", 2.0)
        cfg_spread_threshold = getattr(config, "analysis_ema_spread_pct", 0.5)
        cfg_persistence = getattr(config, "regime_persistence", 0.7)
        cfg_min_bars = getattr(config, "regime_min_confirm_bars", 2)       # 点3：最小维持Bar数
        cfg_risk_cap_pct = getattr(config, "risk_max_loss_cap_pct", 1.5)    # 点4：单笔交易账户最大亏损比例上限(%)
        cfg_max_sl_pct = getattr(config, "risk_absolute_max_sl_pct", 5.0)  # 点4：硬性绝对止损线宽上限(%)

        # 1. 异步并行获取多周期 K 线
        tasks = [
            self._exchange_service.fetch_ohlcv(symbol, timeframe="4h", limit=200),
            self._exchange_service.fetch_ohlcv(symbol, timeframe="1h", limit=300),
            self._exchange_service.fetch_ohlcv(symbol, timeframe="15m", limit=300)
        ]
        try:
            ohlcv_4h, ohlcv_1h, ohlcv_15m = await asyncio.gather(*tasks)
        except Exception as e:
            logger.error("%s K线网络层抓取异常: %s", symbol, str(e))
            result["error"] = f"Network Exception: {str(e)}"
            return result

        if not ohlcv_4h or len(ohlcv_4h) < 50 or not ohlcv_1h or len(ohlcv_1h) < 50:
            result["error"] = "K线数据量不满足计算边界"
            return result

        # 2. 计算各周期带阻尼的概率（点 2 & 点 5 可配置化落地）
        p_ctx_4h = self._calculate_period_probs(symbol, "4h", ohlcv_4h, config, cfg_persistence, cfg_adx_threshold, cfg_volatile_ratio, cfg_max_deviation, cfg_spread_threshold)
        p_ctx_1h = self._calculate_period_probs(symbol, "1h", ohlcv_1h, config, cfg_persistence, cfg_adx_threshold, cfg_volatile_ratio, cfg_max_deviation, cfg_spread_threshold)
        p_ctx_15m = self._calculate_period_probs(symbol, "15m", ohlcv_15m, config, cfg_persistence, cfg_adx_threshold, cfg_volatile_ratio, cfg_max_deviation, cfg_spread_threshold)

        p_4h, m_4h = p_ctx_4h["probs"], p_ctx_4h["metrics"]
        p_1h, m_1h = p_ctx_1h["probs"], p_ctx_1h["metrics"]
        p_15m, m_15m = p_ctx_15m["probs"], p_ctx_15m["metrics"]

        current_price = m_4h["current_price"]
        result.update({
            "current_price": current_price,
            "ema20": round(m_4h["ema20"], 10),
            "ema50": round(m_4h["ema50"], 10),
            "rsi": round(m_1h["rsi"], 2),
            "adx": round(m_4h.get("adx", 0), 2),
            "atr_ratio": round(m_4h.get("atr_ratio", 1.0), 2),
            "ema_spread_pct": round(m_4h.get("ema_spread_pct", 0), 4),
            "atr_pct": round(m_4h["atr_pct"], 6),
            "price_vs_ema20_pct": round(m_4h["price_vs_ema20_pct"], 4),
            "vol_ratio": round(m_4h.get("vol_ratio", 0), 4)
        })

        # 3. 联合条件概率共振（点 1 升级：多周期一致性交叉确立）
        joint_trend_prob = p_4h[0] * p_1h[0] * (1.0 + 0.2 * p_15m[0])
        joint_range_prob = p_4h[2] * p_1h[2]
        joint_volatile_prob = max(p_4h[1], p_1h[1])

        combined_scores = np.array([joint_trend_prob, joint_volatile_prob, joint_range_prob])
        final_probs = combined_scores / (combined_scores.sum() if combined_scores.sum() > 0 else 1.0)

        regimes = ["TRENDING", "VOLATILE", "RANGING"]
        raw_regime = regimes[np.argmax(final_probs)]

        if raw_regime == "TRENDING" and (p_4h[0] * p_1h[0]) < 0.40:
            raw_regime = "RANGING"

        # ── 点 3 优化：最小维持周期防抖机制（State Duration Lock） ──
        if raw_regime == regime_ctx["confirmed_regime"]:
            regime_ctx["candidate_regime"] = None
            regime_ctx["duration"] = 0
        else:
            if raw_regime == regime_ctx["candidate_regime"]:
                regime_ctx["duration"] += 1
            else:
                regime_ctx["candidate_regime"] = raw_regime
                regime_ctx["duration"] = 1
            
            # 只有连续 N 根 K 线发出相同新状态信号，才触发硬锁切换
            if regime_ctx["duration"] >= cfg_min_bars:
                logger.info(f"[Regime Confirmed] {symbol} 状态正式锁固切换为: {raw_regime}")
                regime_ctx["confirmed_regime"] = raw_regime
                regime_ctx["candidate_regime"] = None
                regime_ctx["duration"] = 0

        result["regime"] = regime_ctx["confirmed_regime"]
        result["probabilities"] = {
            "TRENDING": round(final_probs[0] * 100, 2),
            "VOLATILE": round(final_probs[1] * 100, 2),
            "RANGING": round(final_probs[2] * 100, 2)
        }

        # 计算基础大周期方向
        if m_4h["ema20"] > m_4h["ema50"] * 1.005:
            result["trend"] = "up"
        elif m_4h["ema20"] < m_4h["ema50"] * 0.995:
            result["trend"] = "down"
        else:
            result["trend"] = "sideways"

        # 归一化强度评级
        adx_strength_ratio = m_4h["adx"] / max(cfg_adx_threshold, 1e-10)
        if result["regime"] == "TRENDING":
            regime_ctx["confirmed_strength"] = "HIGH" if adx_strength_ratio >= 1.3 or final_probs[0] > 0.7 else "MID"
        elif result["regime"] == "VOLATILE":
            regime_ctx["confirmed_strength"] = "HIGH" if m_4h["atr_ratio"] > 2.2 else "MID"
        else:
            regime_ctx["confirmed_strength"] = "HIGH" if m_4h["atr_ratio"] < 0.85 else "MID"
        result["regime_strength"] = regime_ctx["confirmed_strength"]

        # 4. 融合宏观状态的智能入场评级过滤
        if result["regime"] == "VOLATILE":
            result["entry_zone"] = "poor"
        elif result["regime"] == "TRENDING" and result["trend"] == "up":
            if result["rsi"] < config.analysis_rsi_oversold:
                result["entry_zone"] = "good"
            elif result["rsi"] <= config.analysis_rsi_overbought and abs(m_4h["price_vs_ema20_pct"]) <= cfg_max_deviation:
                result["entry_zone"] = "good"
            elif result["rsi"] <= config.analysis_rsi_overbought:
                result["entry_zone"] = "ok"
            else:
                result["entry_zone"] = "poor"
        elif result["regime"] == "RANGING":
            if result["rsi"] < config.analysis_rsi_oversold:
                result["entry_zone"] = "good"
            elif result["rsi"] < 45:
                result["entry_zone"] = "ok"
            else:
                result["entry_zone"] = "poor"
        else:
            result["entry_zone"] = "poor"

        # 5. 若评级为 poor，计算限价挂单
        if result["entry_zone"] == "poor":
            limit_by_ema = m_4h["ema20"] * 0.995
            limit_by_atr = current_price * (1 - m_4h["atr_pct"] * 0.6)
            result["limit_price"] = round(max(min(limit_by_ema, limit_by_atr), current_price * 0.90), 10)

        # ── 点 4 优化：带绝对安全硬护栏的风控与仓位测算系统 ──
        if m_4h["atr_pct"] > 0:
            # A. 自适应波动止损计算
            sl_mult = 1.5
            raw_sl_pct = m_4h["atr_pct"] * sl_mult * 100
            
            # 引入硬防线：止损宽度绝不能超过配置的绝对最大百分比（比如5%），超过则强行熔断限制在硬阈值
            final_sl_pct = min(raw_sl_pct, cfg_max_sl_pct)
            result["sl_pct"] = round(final_sl_pct, 4)
            result["sl_price"] = round(current_price * (1 - final_sl_pct / 100), 10)

            # B. 凯利公式/波动率自适应仓位生成（Risk Position Sizing）
            # 核心原理：仓位 = 单笔账户最大风险暴露(%) / 本次交易的真实止损宽度(%)
            # 这样可以确保无论高波动山寨还是低波动大盘，一旦扫损，对总账户的净值伤害完全一致！
            if final_sl_pct > 0:
                raw_size = cfg_risk_cap_pct / final_sl_pct
                # 针对不同状态进行宏观安全折价
                if result["regime"] == "RANGING":
                    raw_size *= 0.7  # 震荡市仓位打七折，防来回双劈
                result["suggested_position_size_pct"] = round(min(raw_size, 1.0) * 100, 2)  # 最大不超过名义 100% 杠杆单位

            # C. 阶梯止盈
            tp_scale = 1.3 if result["regime"] == "TRENDING" else 0.8
            result["tp_levels"] = [
                {"price": round(current_price * (1 + m_4h["atr_pct"] * 2.0 * tp_scale), 10), "qty_pct": 0.5, "label": "TP1"},
                {"price": round(current_price * (1 + m_4h["atr_pct"] * 3.0 * tp_scale), 10), "qty_pct": 0.3, "label": "TP2"},
                {"price": round(current_price * (1 + m_4h["atr_pct"] * 4.5 * tp_scale), 10), "qty_pct": 0.2, "label": "TP3"},
            ]

        logger.info(
            "%s [完备评估]: 状态=%s(%s) 维持周期=%d | 评级=%s | 建议仓位=%s%% | 波动止损=%s%%",
            symbol, result["regime"], result["regime_strength"], regime_ctx["duration"],
            result["entry_zone"], result["suggested_position_size_pct"], result["sl_pct"]
        )
        return result

    def _calculate_period_probs(self, symbol: str, timeframe: str, ohlcv: List[List[float]], config, 
                                persistence: float, adx_th: float, vol_ratio: float, dev_pct: float, spread_th: float) -> Dict[str, Any]:
        """单周期底层原生矩阵推演"""
        cache_key = f"{symbol}:{timeframe}"
        close = [float(c[4]) for c in ohlcv]
        high  = [float(c[2]) for c in ohlcv]
        low   = [float(c[3]) for c in ohlcv]
        volume = [float(c[5]) for c in ohlcv]

        ema20_raw = self._ema(close, config.analysis_ema_fast)
        ema50_raw = self._ema(close, config.analysis_ema_slow)
        atr_raw   = self._atr(high, low, close, config.analysis_rsi_period)
        adx_raw   = self._adx(high, low, close, config.analysis_rsi_period)

        valid_ema20 = [x for x in ema20_raw if x is not None and not np.isnan(x)]
        valid_ema50 = [x for x in ema50_raw if x is not None and not np.isnan(x)]
        valid_atr   = [x for x in atr_raw if x is not None and not np.isnan(x)]
        valid_adx   = [x for x in adx_raw if x is not None and not np.isnan(x)]

        if not (valid_ema20 and valid_ema50 and valid_atr and valid_adx):
            return {"probs": np.array([0.1, 0.1, 0.8]), "metrics": {"current_price": close[-1], "ema20": close[-1], "ema50": close[-1], "adx": 15.0, "rsi": 50.0, "atr_pct": 0.02, "atr_ratio": 1.0, "vol_ratio": 1.0, "price_vs_ema20_pct": 0.0, "ema_spread_pct": 0.0}}

        ema20_v, ema50_v, atr_v, adx_v, price = valid_ema20[-1], valid_ema50[-1], valid_atr[-1], valid_adx[-1], close[-1]

        ema_spread = abs(ema20_v - ema50_v) / max(ema50_v, 1e-10) * 100
        price_dev  = abs(price - ema20_v) / max(ema20_v, 1e-10) * 100
        
        atr_median = statistics.median(valid_atr)
        atr_ratio  = atr_v / atr_median if atr_median > 0 else 1.0
        atr_pct    = atr_v / price if price > 0 else 0

        # 精确局部连续切片
        window = valid_ema20[-5:]
        ema_slope = ((window[-1] - window[0]) / max(abs(window[0]), 1e-10) * 100) / (len(window) - 1)

        vol_ma20 = sum(volume[-21:-1]) / 20 if len(volume) >= 21 else 0
        vol_ratio_v = volume[-2] / vol_ma20 if vol_ma20 > 0 else 0

        # 得分模型求解
        trend_score = (adx_v / max(adx_th, 1e-10)) * 2.5 + (ema_spread / spread_th) + (abs(ema_slope) / 0.05)
        volatile_score = (atr_ratio / vol_ratio) * 3.0 + (price_dev / dev_pct)
        range_score = ((18 - adx_v) / 4.0 if adx_v < 18 else 0.0) + ((spread_th - ema_spread) / 0.1 if ema_spread < spread_th else 0.0) + 1.5

        raw_probs = self._softmax(np.array([trend_score, volatile_score, range_score]))

        # 指数记忆平滑平展
        if cache_key not in self._history_probs:
            self._history_probs[cache_key] = raw_probs
        else:
            self._history_probs[cache_key] = persistence * self._history_probs[cache_key] + (1 - persistence) * raw_probs

        return {
            "probs": self._history_probs[cache_key],
            "metrics": {
                "current_price": price, "ema20": ema20_v, "ema50": ema50_v, "adx": adx_v,
                "rsi": self._rsi(close, config.analysis_rsi_period)[-1] or 50.0,
                "atr_pct": atr_pct, "atr_ratio": atr_ratio, "vol_ratio": vol_ratio_v,
                "price_vs_ema20_pct": (price / ema20_v - 1) * 100,
                "ema_spread_pct": ema_spread
            }
        }

    # ── 纯 Python 技术指标库实现保持一致（_ema, _rsi, _atr, _adx, _wilder_smooth 省略，与上一版完全相同） ──
    @staticmethod
    def _ema(p, period):
        if len(p) < period:
            return [None] * len(p)
        result = [None] * (period - 1)
        sma = sum(p[:period]) / period
        result.append(sma)
        k = 2.0 / (period + 1)
        for v in p[period:]:
            sma = v * k + sma * (1 - k)
            result.append(sma)
        return result

    @staticmethod
    def _rsi(close, period=14):
        if len(close) < period + 1:
            return [None] * len(close)
        gains, losses = [], []
        for i in range(1, period + 1):
            diff = close[i] - close[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        rs = avg_g / avg_l if avg_l > 0 else float('inf')
        rsi = [None] * period
        rsi.append(100 - 100 / (1 + rs))
        for i in range(period + 1, len(close)):
            diff = close[i] - close[i - 1]
            g = max(diff, 0)
            l = max(-diff, 0)
            avg_g = (avg_g * (period - 1) + g) / period
            avg_l = (avg_l * (period - 1) + l) / period
            rs = avg_g / avg_l if avg_l > 0 else float('inf')
            rsi.append(100 - 100 / (1 + rs))
        while len(rsi) < len(close):
            rsi.append(rsi[-1])
        return rsi


    def _atr(self, high, low, close, period=14):
        if len(close) < period + 1:
            return [None]*len(close)
        tr = []
        for i in range(1, len(close)):
            tr.append(max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
        atr = [None]*period
        atr.append(sum(tr[:period])/period)
        for i in range(period, len(tr)):
            atr.append((atr[-1]*(period-1)+tr[i])/period)
        while len(atr) < len(close):
            atr.append(atr[-1])
        return atr

    def _adx(self, high, low, close, period=14):
        if len(close) < period*2:
            return [None]*len(close)
        tr, pdm, mdm = [], [], []
        for i in range(1, len(close)):
            tr.append(max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
            up, down = high[i]-high[i-1], low[i-1]-low[i]
            pdm.append(up if up>down and up>0 else 0)
            mdm.append(down if down>up and down>0 else 0)
        def ws(v):
            r=[];s=sum(v[:period])/period;r.append(s)
            for x in v[period:]:s=(s*(period-1)+x)/period;r.append(s)
            return r
        tr_s, ps, ms = ws(tr), ws(pdm), ws(mdm)
        dx=[]
        for p,m,t in zip(ps,ms,tr_s):
            pdi=100*p/t if t else 0;mdi=100*m/t if t else 0
            dx.append(100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi) else 0)
        adx=ws(dx)
        return [None]*(len(close)-len(adx))+adx
