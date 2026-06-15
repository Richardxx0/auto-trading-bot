"""
自动交易机器人 —— 入口。

用法：
    python main.py
    python main.py --env /path/to/.env
"""
import argparse
import asyncio
import logging
import signal
import sys

from config.settings import load_config
from src.telegram_listener import TelegramListener
from src.signal_handler import SignalHandler


def 设置日志(cfg) -> None:
    """配置日志输出。"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def 解析参数() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="YSS 信号自动交易机器人")
    parser.add_argument(
        "--env",
        default=None,
        help=".env 文件路径（默认：项目根目录下的 .env）",
    )
    return parser.parse_args()


async def 主异步函数() -> None:
    """主流程。"""
    args = 解析参数()
    cfg = load_config(env_file=args.env)

    设置日志(cfg)
    log = logging.getLogger("main")

    if not cfg.is_valid:
        log.error("配置无效，请检查 .env 文件")
        sys.exit(1)

    # 打印启动信息
    log.info("=" * 56)
    log.info("  YSS 信号自动交易机器人")
    log.info("  模式: %s", "测试网（模拟）" if cfg.binance_testnet else "实盘")
    log.info("  杠杆: %dx", cfg.leverage)
    log.info("  每笔风险: %.1f%%", cfg.risk_per_trade * 100)
    log.info("  止损: %.1f%%  止盈: %.1f%%",
             cfg.stop_loss_pct * 100, cfg.take_profit_pct * 100)
    log.info("=" * 56)

    handler = SignalHandler(cfg)
    listener = TelegramListener(cfg, on_message=handler.on_telegram_message)

    # 优雅关闭处理
    关闭事件 = asyncio.Event()

    def _信号处理():
        log.info("收到关闭信号，正在停止 ...")
        关闭事件.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _信号处理)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    try:
        # 并发运行监听器和关闭等待
        监听任务 = asyncio.create_task(listener.start())
        await asyncio.wait(
            [监听任务, asyncio.create_task(关闭事件.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as exc:
        log.exception("未捕获的错误: %s", exc)
    finally:
        await listener.stop()
        log.info("机器人已停止")


if __name__ == "__main__":
    asyncio.run(主异步函数())
