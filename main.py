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
from src.web_listener import WebSignalListener
from src.position_monitor import PositionMonitor
from src.exchange import ExchangeClient
from core.exchange_service import ExchangeService
from src.signal_handler import SignalHandler
from dashboard import event_log as el
from dashboard import signal_store as ss


def 设置日志(cfg) -> None:
    """配置日志输出。"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
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


async def _cron_cleanup_task(signal_handler) -> None:
    """cron cleanup at 13/28/43/58"""
    logger = logging.getLogger("cron_cleanup")
    logger.info("cron cleanup started")
    import time
    while True:
        try:
            current_min = time.localtime().tm_min
            if current_min in [13, 28, 43, 58]:
                signal_handler._pre_signal_cleanup()
                await asyncio.sleep(61)
                continue
        except Exception as e:
            logger.exception("cron error: %s", e)
        await asyncio.sleep(10)


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
    log.info("  止损: ATR自适应(上限5%%)  止盈: ATR自适应(上限20%%)")
    log.warning("[Test Mode] regime_min_confirm_bars=" + str(cfg.regime_min_confirm_bars) + " (set to 2+ for live)")

    exchange_client = ExchangeClient(cfg)
    exchange_service = ExchangeService(exchange_client)
    handler = SignalHandler(cfg, exchange_service)

    async def _on_signal_wrapper(symbol, price, alert_count):
        el.write("信号", symbol, "价格=" + str(price) + " 第" + str(alert_count) + "次报警")
        result = await handler.on_web_signal(symbol, price, alert_count)
        # 同步结果到 signals.json
        _all = ss.load_all()
        _target = symbol + "USDT"
        for _i in range(len(_all)-1, -1, -1):
            if _all[_i].get("symbol") == _target:
                _all[_i]["trade_status"] = result
                if result == "已开单":
                    el.write("开仓", symbol, "已开仓 价格=" + str(price))
                elif result == "跳过":
                    el.write("未开", symbol, "跳过 价格=" + str(price))
                else:
                    el.write("未开", symbol, "失败 价格=" + str(price))
                break
        ss.save_all(_all)
    listener = WebSignalListener(
        email=cfg.yss_email,
        password=cfg.yss_password,
        on_signal_callback=_on_signal_wrapper,
    )
    monitor = PositionMonitor(exchange_service, cfg)

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
        监控任务 = asyncio.create_task(monitor.start())
        清洗任务 = asyncio.create_task(_cron_cleanup_task(handler))
        await asyncio.wait(
            [监听任务, 监控任务, 清洗任务, asyncio.create_task(关闭事件.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as exc:
        log.exception("未捕获的错误: %s", exc)
    finally:
        await listener.stop()
        monitor.stop()
        log.info("机器人已停止")


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(主异步函数())
