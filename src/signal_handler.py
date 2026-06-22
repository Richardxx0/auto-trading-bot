"""
信号处理器 —— 编排完整交易流水线。

流程：
  第1步  解析信号
  第2步  检查信号类型
  第3步  币种→合约映射
  第4步  内存去重
  第5步  检查合约存在
  第6步  检查已有持仓
  第7步  获取账户余额
  第8步A 市场状态分类（TRENDING / RANGING / VOLATILE）
  第8步A+ 状态稳定确认（连续2次一致）          ← 新增
  第8步B 技术分析（EMA/RSI/ATR → 入场评级）
  风控联动（仅基于稳定状态生效）
  第9步  决策执行
"""
import asyncio
import json
import logging
import os
import time

from config.settings import Config
from src.parser import SignalParser
from core.exchange_service import ExchangeService
from src.risk_manager import RiskManager
from src.analyzer import TechnicalAnalyzer
from dashboard import trade_store as ts

logger = logging.getLogger(__name__)


class SignalHandler:
    """信号处理器 —— 收到信号后完成解析、技术分析、决策、下单全流程。"""

    def __init__(self, config: Config, exchange_service: ExchangeService):
        self._cfg = config
        self._parser = SignalParser()
        self._exchange_service = exchange_service
        self._exchange = exchange_service._exch
        self._risk = RiskManager(config)
        self._analyzer = TechnicalAnalyzer(self._exchange_service)
        self._dedup: set[str] = set()
        self._dedup_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "dedup.json",
        )
        self._加载去重记录()

        # 市场状态缓存（新增）
        # 结构：{ symbol: {"regime": str, "strength": str, "confirm_count": int, "last_update": float} }
        self._market_state_cache: dict[str, dict] = {}
        self._consecutive_losses = 0
        self._banned_until = 0.0
        self._trade_result: str | None = None

    def _加载去重记录(self) -> None:
        """从文件加载持久化的去重记录。"""
        try:
            with open(self._dedup_file, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._dedup = set(data.keys())
                    logger.info("已加载 %d 条去重记录", len(self._dedup))
        except (FileNotFoundError, json.JSONDecodeError):
            self._dedup = set()

    def _标记去重(self, symbol: str) -> None:
        """标记合约已处理，持久化到文件。"""
        self._dedup.add(symbol)
        try:
            with open(self._dedup_file, "w") as f:
                json.dump({s: time.time() for s in self._dedup}, f, indent=2)
        except Exception as exc:
            logger.warning("去重记录保存失败: %s", exc)

    async def on_telegram_message(self, sender: str, text: str) -> None:
        """Telegram 监听器的回调入口。"""
        try:
            await self._处理信号(sender, text)
        except Exception as exc:
            logger.exception("信号处理器未捕获的错误: %s", exc)
            return "失败"


    async def on_web_signal(self, symbol: str, price: float, alert_count: int = 1) -> str:
        """返回开单结果: 已开单 / 跳过 / 失败"""
        self._trade_result = None
        try:
            # 构造解析后的信号结构（省去第1步自然语言解析）
            signal = {
                "symbol": symbol.upper().strip(),
                "signal_type": "LONG",
                "price": price,
                "alert_count": alert_count,
            }
            await self._处理解析后的信号(signal, "yss-signal.com")
            return self._trade_result or "跳过"
        except Exception as exc:
            logger.exception("信号处理器未捕获的错误: %s", exc)
            return "失败"

    async def _处理信号(self, sender: str, text: str) -> None:
        # ════════════════════════════════════════════
        # 第1步：解析信号
        # ════════════════════════════════════════════
        signal = self._parser.parse(text)
        if signal is None:
            return

        coin = signal["symbol"]
        signal_type = signal["signal_type"]
        msg_price = signal.get("price")
        alert_count = signal.get("alert_count", 0)
        # 逐级加严：根据预警次数动态调整核实力度
        if alert_count >= 10:
            scr_level = 3
        elif alert_count >= 6:
            scr_level = 2
        elif alert_count >= 2:
            scr_level = 1
        else:
            scr_level = 0
        min_confirm = getattr(self._cfg,"regime_min_confirm_bars",2) + scr_level
        vol_mult = 1.0 + 0.5 * scr_level
        if scr_level > 0:
            logger.info("【逐级加严】预警次数=%d, 力度等级=%d, 需confirm>=%d, 成交量乘数=%.1f",
                        alert_count, scr_level, min_confirm, vol_mult)

        logger.info("=" * 56)
        logger.info("【第1步】收到信号: 币种=%s 类型=%s 发送者=%s",
                    coin, signal_type, sender)
        if msg_price:
            logger.info("  消息中包含入场价: %.6f", msg_price)
        logger.info("  原始消息: %s", text[:150])

        # ════════════════════════════════════════════
        # 第2步：检查信号类型
        # ════════════════════════════════════════════
        if signal_type.upper() != "LONG":
            logger.info("【第2步】信号类型 '%s' 暂不支持，跳过", signal_type)
            return
        logger.info("【第2步】信号类型检查通过: LONG")

        # ════════════════════════════════════════════
        # 第3步：币种映射
        # ════════════════════════════════════════════
        contract_symbol = self._cfg.coin_mapping.get(coin)
        if contract_symbol is None:
            auto_sym = f"{coin}USDT"
            if self._exchange.contract_exists(auto_sym):
                contract_symbol = auto_sym
                logger.info("【第3步】自动映射 %s -> %sUSDT", coin, coin)
            else:
                logger.warning("【第3步】未找到币种 '%s' 的合约映射，跳过", coin)
                return
        logger.info("【第3步】映射 %s -> %s", coin, contract_symbol)


        # ════════════════════════════════════════════
        # 第5步：检查合约存在
        # ════════════════════════════════════════════
        if not self._exchange.contract_exists(contract_symbol):
            logger.warning("【第5步】合约 %s 在 Binance 合约市场不存在，跳过", contract_symbol)
            return
        logger.info("【第5步】合约存在检查通过")

        # ════════════════════════════════════════════
        # 第6步：检查持仓
        # ════════════════════════════════════════════
        if self._exchange.has_open_position(contract_symbol):
            logger.info("【第6步】合约 %s 已有持仓，跳过重复开仓", contract_symbol)
            self._标记去重(contract_symbol)
            return
        logger.info("【第6步】持仓检查通过（无持仓）")

        # ════════════════════════════════════════════
        # 第7步：获取余额
        # ════════════════════════════════════════════
        balance = self._exchange.get_balance_usdt()
        if balance <= 0:
            logger.error("【第7步】账户余额为 0 或获取失败，中止")
            return
        logger.info("【第7步】账户余额: %.2f USDT", balance)

        # ════════════════════════════════════════════
        # 风控：检查最大持仓数
        # ════════════════════════════════════════════
        open_count = self._exchange.get_open_positions_count()
        if open_count >= self._cfg.max_open_positions:
            logger.info(
                "【风控】已达最大持仓数 %d/%d，跳过 %s",
                open_count, self._cfg.max_open_positions, contract_symbol,
            )
            self._标记去重(contract_symbol)
            return

        # ════════════════════════════════════════════
        # 第8步A：市场状态分类
        # ════════════════════════════════════════════
        logger.info("【第8步A】开始市场状态分类（获取4hK线 + ADX + ATR比值）...")
        analysis = await self._analyzer.analyze(contract_symbol, self._cfg)
        raw_regime = analysis.get("regime","RANGING")
        raw_strength = analysis.get("regime_strength", "MEDIUM")

        logger.info(
            "【第8步A】原始市场状态: %s  强度=%s  ADX=%s  ATR比值=%s  EMA发散=%.2f%%",
            raw_regime, raw_strength,
            analysis.get("adx", "N/A"),
            analysis.get("atr_ratio", "N/A"),
            analysis.get("ema_spread_pct", 0),
        )

        if analysis.get("adx",0) is None:
            logger.warning("【第8步A】ADX数据不足，状态分类可能不准确")

        # ════════════════════════════════════════════
        # 第8步A+：市场状态稳定确认（新增）
        # ════════════════════════════════════════════
        confirmed_state = self._确认市场状态(
            contract_symbol, raw_regime, raw_strength,
        )

        if confirmed_state is None:
            logger.warning(
                "【第8步A+】市场状态未稳定（confirm < min_confirm），跳过本次交易",
            )
            self._标记去重(contract_symbol)
            return

        # 使用稳定后的状态进行后续风控
        regime = confirmed_state["regime"]
        regime_strength = confirmed_state["strength"]
        confirm_count = confirmed_state["confirm_count"]

        logger.info(
            "【第8步A+】市场状态已稳定: %s  强度=%s  confirm=%d",
            regime, regime_strength, confirm_count,
        )

        # ════════════════════════════════════════════
        # 第8步B：技术分析
        # ════════════════════════════════════════════
        logger.info("【第8步B】开始技术分析（获取K线 + 计算EMA/RSI/ATR）...")
        

        # 成交量确认
        if self._cfg.volume_confirm_enabled and not analysis.get("error"):
            vol_r = analysis.get("vol_ratio", 0)
            if vol_r < self._cfg.volume_min_ratio * vol_mult:
                logger.info("【成交量过滤】%s vol=%.2f < 最低要求%.2f（力度%d倍），跳过\nanalysis keys: %s",
                            contract_symbol, vol_r, self._cfg.volume_min_ratio * vol_mult, vol_mult, list(analysis.keys()))
                return

        if analysis.get("error"):
            logger.warning(
                "【第8步B】技术分析失败（%s），降级为市价开仓",
                analysis["error"],
            )
            await self._执行市价开仓(contract_symbol, msg_price, balance)
            self._标记去重(contract_symbol)
            return

        entry_zone = analysis.get("entry_zone","poor")
        current_price = analysis.get("current_price", 0)

        logger.info(
            "【第8步B】技术分析: 趋势=%s RSI=%s 入场评级=%s",
            analysis.get("trend", "?"),
            analysis.get("rsi", "?"),
            entry_zone,
        )

        # 逐级加严：预警次数高时提高入场评级要求
        if scr_level >= 2 and entry_zone == "poor":
            logger.info("【逐级加严】预警次数=%d(力度%d), 评级poor不达标, 跳过", alert_count, scr_level)
            return

        # ════════════════════════════════════════════
        # 风控联动（基于稳定后的市场状态）
        # ════════════════════════════════════════════
        risk_multiplier = 1.0

        if regime == "RANGING":
            logger.info(
                "【风控联动】震荡行情（稳定状态）→ 禁止追涨，强制使用限价单",
            )
            entry_zone = "poor"
            if analysis.get("limit",0) is None or analysis["limit_price"] <= 0:
                analysis["limit_price"] = current_price * 0.995
                logger.info(
                    "【风控联动】震荡行情限价设为: %.8f（市价下方0.5%%）",
                    analysis["limit_price"],
                )

        elif regime == "VOLATILE":
            risk_multiplier = 0.5
            logger.info(
                "【风控联动】高波动行情（稳定状态）→ 仓位减半（乘数=%.1f）",
                risk_multiplier,
            )

        elif regime == "TRENDING":
            logger.info(
                "【风控联动】趋势行情（稳定状态）→ 允许市价单，强度=%s",
                regime_strength,
            )

        # ════════════════════════════════════════════
        # 第9步：决策执行
        # ════════════════════════════════════════════
        if entry_zone in ("good", "ok"):
            logger.info("【第9步】入场评级=%s → 市价开多", entry_zone)
            self._trade_result = "已开单"
            await self._执行市价开仓(
                contract_symbol, msg_price, balance, risk_multiplier, analysis,
            )

        elif entry_zone == "poor":
            limit_price = analysis.get("limit",0)
            if limit_price is None or limit_price <= 0:
                logger.warning(
                    "【第9步】入场评级=poor 但无有效限价，降级为市价开仓",
                )
                self._trade_result = "已开单"
                await self._执行市价开仓(
                    contract_symbol, msg_price, balance, risk_multiplier, analysis,
                )
            else:
                logger.info(
                    "【第9步】入场评级=poor → 挂限价单 %.8f （当前价 %.8f）",
                    limit_price, current_price,
                )
                self._trade_result = "已开单"
                await self._执行限价开仓(
                    contract_symbol, limit_price, msg_price,
                    balance, risk_multiplier,
                )

        else:
            logger.warning("【第9步】入场评级=%s 未知，跳过", entry_zone)

        self._标记去重(contract_symbol)

    # ── 市场状态稳定确认（新增） ─────────────────────────────


    async def _处理解析后的信号(self, signal: dict, sender: str = "") -> None:
        """处理已解析的信号对象（从第2步开始）。"""
        # 熔断检查
        if time.time() < self._banned_until:
            remaining = (self._banned_until - time.time()) / 3600
            logger.info("【熔断中】暂停交易，剩余 %.1f 小时", remaining)
            return

        coin = signal["symbol"]
        signal_type = signal["signal_type"]
        msg_price = signal.get("price")
        alert_count = signal.get("alert_count", 0)

        # 逐级加严：根据预警次数动态调整核实力度
        if alert_count >= 10:
            scr_level = 3
        elif alert_count >= 6:
            scr_level = 2
        elif alert_count >= 2:
            scr_level = 1
        else:
            scr_level = 0
        min_confirm = getattr(self._cfg,"regime_min_confirm_bars",2) + scr_level
        vol_mult = 1.0 + 0.5 * scr_level
        if scr_level > 0:
            logger.info("【逐级加严】预警次数=%d, 力度等级=%d, 需confirm>=%d, 成交量乘数=%.1f",
                        alert_count, scr_level, min_confirm, vol_mult)

        logger.info("=" * 56)
        logger.info("【第1步跳过】收到信号: 币种=%s 类型=%s 第%d次报警 来源=%s",
                     coin, signal_type, alert_count, sender)
        if msg_price:
            logger.info("  入场价: %.6f", msg_price)

        # ════════════════════════════════════════════
        # 第2步：检查信号类型
        # ════════════════════════════════════════════
        if signal_type.upper() != "LONG":
            logger.info("【第2步】信号类型 '%s' 暂不支持，跳过", signal_type)
            return
        logger.info("【第2步】信号类型检查通过: LONG")

        # ════════════════════════════════════════════
        # 第3步：币种映射
        # ════════════════════════════════════════════
        contract_symbol = self._cfg.coin_mapping.get(coin)
        if contract_symbol is None:
            auto_sym = f"{coin}USDT"
            if self._exchange.contract_exists(auto_sym):
                contract_symbol = auto_sym
                logger.info("【第3步】自动映射 %s -> %sUSDT", coin, coin)
            else:
                logger.warning("【第3步】未找到币种 '%s' 的合约映射，跳过", coin)
                return
        logger.info("【第3步】映射 %s -> %s", coin, contract_symbol)


        # ════════════════════════════════════════════
        # 第5步：检查合约存在
        # ════════════════════════════════════════════
        if not self._exchange.contract_exists(contract_symbol):
            logger.warning("【第5步】合约 %s 在 Binance 合约市场不存在，跳过", contract_symbol)
            return
        logger.info("【第5步】合约存在检查通过")

        # ════════════════════════════════════════════
        # 第6步：检查持仓
        # ════════════════════════════════════════════
        if self._exchange.has_open_position(contract_symbol):
            logger.info("【第6步】合约 %s 已有持仓，跳过重复开仓", contract_symbol)
            self._标记去重(contract_symbol)
            return
        logger.info("【第6步】持仓检查通过（无持仓）")

        # ════════════════════════════════════════════
        # 第7步：获取余额
        # ════════════════════════════════════════════
        balance = self._exchange.get_balance_usdt()
        if balance <= 0:
            logger.error("【第7步】账户余额为 0 或获取失败，中止")
            return
        logger.info("【第7步】账户余额: %.2f USDT", balance)

        # ════════════════════════════════════════════
        # 风控：检查最大持仓数
        # ════════════════════════════════════════════
        open_count = self._exchange.get_open_positions_count()
        if open_count >= self._cfg.max_open_positions:
            logger.info(
                "【风控】已达最大持仓数 %d/%d，跳过 %s",
                open_count, self._cfg.max_open_positions, contract_symbol,
            )
            self._标记去重(contract_symbol)
            return

        # ════════════════════════════════════════════
        # 第8步A：市场状态分类
        # ════════════════════════════════════════════
        logger.info("【第8步A】开始市场状态分类（获取4hK线 + ADX + ATR比值）...")
        analysis = await self._analyzer.analyze(contract_symbol, self._cfg)
        raw_regime = analysis.get("regime","RANGING")
        raw_strength = analysis.get("regime_strength", "MEDIUM")

        logger.info(
            "【第8步A】原始市场状态: %s  强度=%s  ADX=%s  ATR比值=%s  EMA发散=%.2f%%",
            raw_regime, raw_strength,
            analysis.get("adx", "N/A"),
            analysis.get("atr_ratio", "N/A"),
            analysis.get("ema_spread_pct", 0),
        )

        if analysis.get("adx",0) is None:
            logger.warning("【第8步A】ADX数据不足，状态分类可能不准确")

        # ════════════════════════════════════════════
        # 第8步A+：市场状态稳定确认（新增）
        # ════════════════════════════════════════════
        confirmed_state = self._确认市场状态(
            contract_symbol, raw_regime, raw_strength,
        )

        if confirmed_state is None:
            logger.warning(
                "【第8步A+】市场状态未稳定（confirm < min_confirm），跳过本次交易",
            )
            self._标记去重(contract_symbol)
            return

        # 使用稳定后的状态进行后续风控
        regime = confirmed_state["regime"]
        regime_strength = confirmed_state["strength"]
        confirm_count = confirmed_state["confirm_count"]

        logger.info(
            "【第8步A+】市场状态已稳定: %s  强度=%s  confirm=%d",
            regime, regime_strength, confirm_count,
        )

        # ════════════════════════════════════════════
        # 第8步B：技术分析
        # ════════════════════════════════════════════
        logger.info("【第8步B】开始技术分析（获取K线 + 计算EMA/RSI/ATR）...")
        

        # 成交量确认
        if self._cfg.volume_confirm_enabled and not analysis.get("error"):
            vol_r = analysis.get("vol_ratio", 0)
            if vol_r < self._cfg.volume_min_ratio * vol_mult:
                logger.info("【成交量过滤】%s vol=%.2f < 最低要求%.2f（力度%d個），跳过\nanalysis keys: %s",
                            contract_symbol, vol_r, self._cfg.volume_min_ratio * vol_mult, vol_mult, list(analysis.keys()))
                return

        if analysis.get("error"):
            logger.warning(
                "【第8步B】技术分析失败（%s），降级为市价开仓",
                analysis["error"],
            )
            await self._执行市价开仓(contract_symbol, msg_price, balance)
            self._标记去重(contract_symbol)
            return

        entry_zone = analysis.get("entry_zone","poor")
        current_price = analysis.get("current_price", 0)

        logger.info(
            "【第8步B】技术分析: 趋势=%s RSI=%s 入场评级=%s",
            analysis.get("trend", "?"),
            analysis.get("rsi", "?"),
            entry_zone,
        )

        # 逐级加严：预警次数高时提高入场评级要求
        if scr_level >= 2 and entry_zone == "poor":
            logger.info("【逐级加严】预警次数=%d(力度%d), 评级poor不达标, 跳过", alert_count, scr_level)
            return

        # ════════════════════════════════════════════
        # 风控联动（基于稳定后的市场状态）
        # ════════════════════════════════════════════
        risk_multiplier = 1.0

        if regime == "RANGING":
            logger.info(
                "【风控联动】震荡行情（稳定状态）→ 禁止追涨，强制使用限价单",
            )
            entry_zone = "poor"
            if analysis.get("limit",0) is None or analysis["limit_price"] <= 0:
                analysis["limit_price"] = current_price * 0.995
                logger.info(
                    "【风控联动】震荡行情限价设为: %.8f（市价下方0.5%%）",
                    analysis["limit_price"],
                )

        elif regime == "VOLATILE":
            risk_multiplier = 0.5
            logger.info(
                "【风控联动】高波动行情（稳定状态）→ 仓位减半（乘数=%.1f）",
                risk_multiplier,
            )

        elif regime == "TRENDING":
            logger.info(
                "【风控联动】趋势行情（稳定状态）→ 允许市价单，强度=%s",
                regime_strength,
            )

        # ════════════════════════════════════════════
        # 第9步：决策执行
        # ════════════════════════════════════════════
        if entry_zone in ("good", "ok"):
            logger.info("【第9步】入场评级=%s → 市价开多", entry_zone)
            self._trade_result = "已开单"
            await self._执行市价开仓(
                contract_symbol, msg_price, balance, risk_multiplier, analysis,
            )

        elif entry_zone == "poor":
            limit_price = analysis.get("limit",0)
            if limit_price is None or limit_price <= 0:
                logger.warning(
                    "【第9步】入场评级=poor 但无有效限价，降级为市价开仓",
                )
                self._trade_result = "已开单"
                await self._执行市价开仓(
                    contract_symbol, msg_price, balance, risk_multiplier, analysis,
                )
            else:
                logger.info(
                    "【第9步】入场评级=poor → 挂限价单 %.8f （当前价 %.8f）",
                    limit_price, current_price,
                )
                self._trade_result = "已开单"
                await self._执行限价开仓(
                    contract_symbol, limit_price, msg_price,
                    balance, risk_multiplier,
                )

        else:
            logger.warning("【第9步】入场评级=%s 未知，跳过", entry_zone)

        self._标记去重(contract_symbol)

    def _确认市场状态(
        self,
        symbol: str,
        raw_regime: str,
        raw_strength: str,
        min_confirm: int = 1,
    ) -> dict | None:
        """市场状态稳定确认。

        规则：
        - 同一 symbol 的状态必须连续 2 次一致才确认生效
        - 否则保持上一稳定状态
        - 未确认时返回 None，调用方应跳过交易

        返回确认后的状态字典:
          {"regime": str, "strength": str, "confirm_count": int}
        """
        now = time.time()
        cached = self._market_state_cache.get(symbol)

        # 场景A：首次见到该 symbol
        if cached is None:
            self._market_state_cache[symbol] = {
                "regime": raw_regime,
                "strength": raw_strength,
                "confirm_count": 1,
                "last_update": now,
            }
            logger.info(
                "[REGIME UPDATE] %s %s confirm=1/%d（首次）",
                symbol, raw_regime, min_confirm,
            )
            if min_confirm <= 1:
                return self._market_state_cache[symbol]
            return None
        last_regime = cached["regime"]

        # 场景B：与上次状态一致 → 增加确认计数
        if raw_regime == last_regime:
            cached["confirm_count"] += 1
            cached["last_update"] = now

            if cached["confirm_count"] >= min_confirm:
                logger.info(
                    "[REGIME UPDATE] %s %s confirm=%d（已稳定）",
                    symbol, raw_regime, cached["confirm_count"],
                )
                return {
                    "regime": last_regime,
                    "strength": raw_strength,
                    "confirm_count": cached["confirm_count"],
                }

            logger.info(
                "[REGIME UPDATE] %s %s confirm=%d/%d",
                symbol, raw_regime, cached["confirm_count"], min_confirm,
            )
            return None

        # 场景C：状态发生变化 → 重置计数，重新累计
        self._market_state_cache[symbol] = {
            "regime": raw_regime,
            "strength": raw_strength,
            "confirm_count": 1,
            "last_update": now,
        }
        logger.info(
            "[REGIME UPDATE] %s %s -> %s confirm=1/%d（切换）",
                symbol, last_regime, raw_regime, min_confirm,
            )
        return None

    # ── 执行方法 ─────────────────────────────────────────────

    async def _执行市价开仓(
        self,
        symbol: str,
        msg_price: float | None,
        balance: float,
        risk_multiplier: float = 1.0,
        analysis: dict | None = None,
    ) -> None:
        """获取入场价 → 计算仓位（×风险乘数）→ 市价开多 → 止盈止损。"""
        if msg_price and msg_price > 0:
            entry_price = msg_price
            logger.info("  使用消息中的价格作为入场价: %.6f", entry_price)
        else:
            entry_price = self._exchange.get_current_price(symbol)
            if entry_price is None or entry_price <= 0:
                logger.error("  无法获取 %s 的当前价格，中止", symbol)
                return
            logger.info("  使用实时市场价格作为入场价: %.8f", entry_price)

        # 先计算仓位（始终需要 risk_manager）
        risk_result = self._risk.calculate(
            balance_usdt=balance,
            entry_price=entry_price,
            direction="BUY",
        )
        raw_qty = risk_result["qty"]
        sl_price = risk_result["sl"]

        # 再用分析结果中的动态 SL/TP 覆盖（如果有）
        if analysis and analysis.get("sl_price") and analysis.get("tp_levels"):
            sl_price = analysis["sl_price"]
            tp_levels = analysis["tp_levels"]
            logger.info("  使用动态SL/TP（基于ATR）: SL=%.8f  TP=%s",
                        sl_price,
                        ", ".join(f"{l[chr(39)+chr(108)+chr(97)+chr(98)+chr(101)+chr(108)+chr(39)]}={l[chr(39)+chr(112)+chr(114)+chr(105)+chr(99)+chr(101)+chr(39)]}"
                                  for l in tp_levels))
        else:
            # 无分析数据时，退回到固定 TP
            tp_levels = [{"price": risk_result["tp"], "qty_pct": 1.0, "label": "TP"}]
            tp_price = risk_result["tp"]
            logger.info("  使用固定SL/TP: SL=%.8f TP=%.8f",
                        sl_price, risk_result["tp"])

        if raw_qty <= 0:
            logger.warning("  计算出的数量为 0，中止")
            return

        qty = raw_qty * risk_multiplier
        logger.info(
            "  仓位: 原始数量=%.4f  风险乘数=%.1f  最终数量=%.4f  "
            "止损=%.8f (-%.1f%%)  止盈=%.8f (+%.1f%%)",
            raw_qty, risk_multiplier, qty,
            sl_price, self._cfg.stop_loss_pct * 100,
            risk_result["tp"], self._cfg.take_profit_pct * 100,
        )

        logger.info("  执行市价开多 %s ...", symbol)
        result = self._exchange.open_long_market(
            symbol=symbol,
            quantity=qty,
            stop_loss_price=sl_price,
            tp_levels=tp_levels,
        )

        if "error" in result and result["error"]:
            logger.error("!!! 市价开仓失败 %s: %s", symbol, result["error"])
        else:
            logger.info("!!! 市价开仓成功 %s", symbol)
            # 记录交易到仪表盘
            ts.add_trade({
                "symbol": symbol,
                "direction": "LONG",
                "entry_price": entry_price,
                "sl": sl_price,
                "tp": tp_price,
                "qty": qty,
            })
            logger.info("  开仓订单号: %s",
                        result.get("entry", {}).get("id", "N/A"))
            logger.info("  止损订单号: %s",
                        result.get("stop_loss", {}).get("id", "N/A")
                        if result.get("stop_loss") else "失败")
            logger.info("  止盈订单号: %s",
                        result.get("take_profit", {}).get("id", "N/A")
                        if result.get("take_profit") else "失败")

    async def _执行限价开仓(
        self,
        symbol: str,
        limit_price: float,
        msg_price: float | None,
        balance: float,
        risk_multiplier: float = 1.0,
    ) -> None:
        """挂限价买入单 → 后台监控 → 成交后设止盈止损。"""
        entry_price = limit_price

        # 优先使用分析结果中的动态 SL/TP，否则回退到固定百分比
        if analysis and analysis.get("sl_price") and analysis.get("tp_levels"):
            sl_price = analysis["sl_price"]
            tp_levels = analysis["tp_levels"]
            logger.info("  使用动态SL/TP（基于ATR）: SL=%.8f  TP=%s",
                        sl_price,
                        ", ".join(f"{l['label']}={l['price']}"
                                  for l in tp_levels))
        else:
            risk_result = self._risk.calculate(
                balance_usdt=balance,
                entry_price=entry_price,
                direction="BUY",
            )
            sl_price = risk_result["sl"]
            # 无分析数据时，退回到单个固定 TP
            tp_levels = [{"price": risk_result["tp"], "qty_pct": 1.0, "label": "TP"}]
            logger.info("  使用固定SL/TP（分析数据不可用）: SL=%.8f TP=%.8f",
                        sl_price, risk_result["tp"])

        if raw_qty <= 0:
            logger.warning("  计算出的数量为 0，中止限价开仓")
            return

        qty = raw_qty * risk_multiplier
        logger.info(
            "  目标仓位: 原始数量=%.4f  风险乘数=%.1f  最终数量=%.4f  "
            "限价=%.8f  止损=%.8f  止盈=%.8f",
            raw_qty, risk_multiplier, qty,
            limit_price, sl_price, tp_price,
        )

        order = self._exchange.open_long_limit(
            symbol=symbol,
            quantity=qty,
            limit_price=limit_price,
        )

        if "error" in order:
            logger.error("!!! 限价挂单失败 %s: %s", symbol, order["error"])
            return

        order_id = order.get("id", "")
        if not order_id:
            logger.error("!!! 限价挂单未返回订单ID，无法监控")
            return

        # 记录交易到仪表盘（限价挂单，待成交）
        ts.add_trade({
            "symbol": symbol,
            "direction": "LONG",
            "entry_price": limit_price,
            "sl": sl_price,
            "tp": tp_price,
            "qty": qty,
            "status": "LIMIT_PENDING",
        })
        logger.info("!!! 限价挂单成功 %s 订单号=%s 价格=%s",
                     symbol, order_id, limit_price)

        logger.info(
            "  启动后台监控（超时=%ds，检查间隔=%ds）...",
            self._cfg.limit_order_timeout,
            self._cfg.limit_order_check_interval,
        )
        asyncio.create_task(
            self._监控限价单(symbol, order_id, qty, sl_price, tp_price)
        )

    async def _监控限价单(
        self,
        symbol: str,
        order_id: str,
        qty: float,
        sl_price: float,
        tp_price: float,
    ) -> None:
        """后台任务：定期轮询限价单状态，成交后挂止盈止损。"""
        timeout = self._cfg.limit_order_timeout
        interval = self._cfg.limit_order_check_interval
        elapsed = 0

        logger.info("【限价监控】开始监控 %s 订单 %s", symbol, order_id)

        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval

            order = self._exchange.fetch_order_status(symbol, order_id)
            status = order.get("status", "unknown")

            if status in ("closed", "filled"):
                filled_qty = float(order.get("filled", qty))
                fill_price = float(
                    order.get("average", order.get("price", 0))
                )
                logger.info(
                    "【限价监控】>>> 限价单成交! %s 成交价=%.8f 数量=%s",
                    symbol, fill_price, filled_qty,
                )

                logger.info("【限价监控】成交后设置止盈止损 ...")
                sl_result = self._exchange.set_stop_loss_take_profit(
                    symbol=symbol,
                    quantity=filled_qty,
                    stop_loss_price=sl_price,
                    take_profit_price=tp_price,
                )
                if sl_result.get("stop_loss"):
                    logger.info("【限价监控】止损挂单成功")
                else:
                    logger.warning("【限价监控】止损挂单失败")
                if sl_result.get("take_profit"):
                    logger.info("【限价监控】止盈挂单成功")
                else:
                    logger.warning("【限价监控】止盈挂单失败")

                logger.info("【限价监控】限价开仓流程完成 %s", symbol)
                return

            elif status in ("canceled", "expired", "rejected"):
                logger.warning(
                    "【限价监控】订单 %s 状态=%s 已终结", order_id, status,
                )
                return

            elif status == "open":
                logger.info(
                    "【限价监控】限价单 %s 未成交，已等 %ds / %ds",
                    order_id, elapsed, timeout,
                )

            else:
                logger.debug("【限价监控】订单 %s 状态=%s", order_id, status)

        logger.warning(
            "【限价监控】限价单 %s 超过 %ds 未成交，取消",
            order_id, timeout,
        )
        self._exchange.cancel_order(symbol, order_id)
        logger.info("【限价监控】限价开仓流程结束（超时取消）")



