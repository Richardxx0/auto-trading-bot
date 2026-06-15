"""
从环境变量或 .env 文件加载配置。
"""
import os
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _resolve_path(*parts: str) -> str:
    """将路径解析为项目根目录下的路径（从本文件向上两级即为项目根目录）。"""
    return str(Path(__file__).resolve().parent.parent / Path(*parts))


@dataclass
class Config:
    """应用配置数据类，所有字段均有默认值。"""

   # ── Telegram 配置 ─────────────────────────────────────────
   telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
   # 频道用户名（如 @ychan_signal）或 ID；留空则监听所有对话
   telegram_channel: str = ""
 
     # ── YSS 网页信号配置 ──────────────────────────────────────
     yss_email: str = ""
     yss_password: str = ""

    # ── Binance 配置 ──────────────────────────────────────────
    binance_api_key: str = ""
    binance_secret_key: str = ""
    # 是否使用 Binance 合约测试网
    binance_testnet: bool = True

    # ── 交易参数 ──────────────────────────────────────────────
    # 每笔交易风险比例（0.02 = 账户余额的 2%）
    risk_per_trade: float = 0.02
    # 止损百分比（0.02 = 入场价的 2%）
    stop_loss_pct: float = 0.02
    # 止盈百分比（0.04 = 入场价的 4%），风险收益比 1:2
    take_profit_pct: float = 0.04
    # 杠杆倍数
    leverage: int = 5

    # ── 币种 → 合约名映射 ────────────────────────────────────
    # Key 为信号中解析出的币名，Value 为 Binance USDT-M 永续合约名
    coin_mapping: dict = field(default_factory=lambda: {
        "CLO": "CLOUSDT",
        "OPEN": "OPENUSDT",
        "SAHARA": "SAHARAUSDT",
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "BNB": "BNBUSDT",
        "SOL": "SOLUSDT",
        "DOGE": "DOGEUSDT",
    })

    # ── 技术分析参数 ──────────────────────────────
    # K线周期（用于主趋势判断）
    analysis_timeframe: str = "4h"
    # EMA 快线周期
    analysis_ema_fast: int = 20
    # EMA 慢线周期
    analysis_ema_slow: int = 50
    # RSI 周期
    analysis_rsi_period: int = 14
    # RSI 超卖阈值（低于此值视为超卖，适合入场）
    analysis_rsi_oversold: float = 35.0
    # RSI 超买阈值（高于此值视为超买，等回调）
    analysis_rsi_overbought: float = 55.0
    # 价格偏离 EMA(20) 的最大容忍百分比（超出此值视为偏离过大）
    analysis_max_deviation_pct: float = 5.0
    # ADX 阈值（高于此值视为趋势行情）
    analysis_adx_threshold: float = 25.0
    # ATR 比值阈值（高于此值视为高波动行情）
    analysis_volatile_atr_ratio: float = 1.5
    # 限价单监控超时（秒）
    limit_order_timeout: int = 7200
    # 限价单检查间隔（秒）
    limit_order_check_interval: int = 30

    # ── 运行参数 ──────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = ""  # 空字符串表示仅输出到 stderr

    @property
    def is_valid(self) -> bool:
        """检查必填配置项是否已填写。"""
        errors: list[str] = []
        if not self.binance_api_key:
            errors.append("BINANCE_API_KEY 为必填项")
        if not self.binance_secret_key:
            errors.append("BINANCE_SECRET_KEY 为必填项")
         if not self.yss_email:
             errors.append("YSS_EMAIL 为必填项")
         if not self.yss_password:
             errors.append("YSS_PASSWORD 为必填项")

        if errors:
            for e in errors:
                logger.error("配置缺失: %s", e)
            return False
        return True


def _env_bool(key: str, default: bool = False) -> bool:
    """将环境变量字符串解析为布尔值。"""
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def load_config(env_file: str | None = None) -> Config:
    """加载配置。

    优先读取指定 .env 文件（可选），再用进程环境变量覆盖，
    确保敏感信息可以灵活管理。
    """
    # 1. 尝试加载 .env 文件
    if env_file is None:
        env_file = _resolve_path(".env")
    env_path = Path(env_file)
    if env_path.is_file():
        logger.info("从 %s 加载环境变量", env_path)
        _load_dotenv(env_path)
    else:
        logger.info("未找到 .env 文件（%s），仅使用系统环境变量", env_path)

    # 2. 构造配置对象
    cfg = Config(
        telegram_api_id=int(os.environ.get("TELEGRAM_API_ID", "0")),
        telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", ""),
        telegram_phone=os.environ.get("TELEGRAM_PHONE", ""),
        telegram_channel=os.environ.get("TELEGRAM_CHANNEL", ""),
         yss_email=os.environ.get("YSS_EMAIL", ""),
         yss_password=os.environ.get("YSS_PASSWORD", ""),
        binance_api_key=os.environ.get("BINANCE_API_KEY", ""),
        binance_secret_key=os.environ.get("BINANCE_SECRET_KEY", ""),
        binance_testnet=_env_bool("BINANCE_TESTNET", True),
        risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.02")),
        stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.02")),
        take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "0.04")),
        leverage=int(os.environ.get("LEVERAGE", "5")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        analysis_timeframe=os.environ.get("ANALYSIS_TIMEFRAME", "4h"),
        analysis_ema_fast=int(os.environ.get("ANALYSIS_EMA_FAST", "20")),
        analysis_ema_slow=int(os.environ.get("ANALYSIS_EMA_SLOW", "50")),
        analysis_rsi_period=int(os.environ.get("ANALYSIS_RSI_PERIOD", "14")),
        analysis_rsi_oversold=float(os.environ.get("ANALYSIS_RSI_OVERSOLD", "35.0")),
        analysis_rsi_overbought=float(os.environ.get("ANALYSIS_RSI_OVERBOUGHT", "55.0")),
        analysis_max_deviation_pct=float(os.environ.get("ANALYSIS_MAX_DEVIATION_PCT", "5.0")),
        analysis_adx_threshold=float(os.environ.get("ANALYSIS_ADX_THRESHOLD", "25.0")),
        analysis_volatile_atr_ratio=float(os.environ.get("ANALYSIS_VOLATILE_ATR_RATIO", "1.5")),
        limit_order_timeout=int(os.environ.get("LIMIT_ORDER_TIMEOUT", "7200")),
        limit_order_check_interval=int(os.environ.get("LIMIT_ORDER_CHECK_INTERVAL", "30")),
        log_file=os.environ.get("LOG_FILE", ""),
    )

    return cfg


def _load_dotenv(path: Path) -> None:
    """简易 .env 文件解析器（无外部依赖）。

    将文件中的键值对注入进程环境变量。已存在的变量不会被覆盖。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("无法读取 %s: %s", path, exc)
        return

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = val

