"""
自动交易机器人看门狗

用法:
    python watchdog.py
    python watchdog.py --no-dashboard
    python watchdog.py --no-bot
    python watchdog.py --verbose      # 同时显示子进程日志
"""

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | WATCHDOG | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

MAX_RESTARTS = 10
RESTART_BASE_DELAY = 2
RESTART_MAX_DELAY = 60
HEARTBEAT_INTERVAL = 5
STARTUP_DELAY = 3


class ManagedProcess:

    def __init__(self, name, cmd, log_file=None, workdir=None, env=None):
        self.name = name
        self.cmd = cmd
        self.workdir = workdir or PROJECT_ROOT
        self.env = {**os.environ, **(env or {})}
        self.process = None
        self.restart_count = 0
        self.last_start = 0.0
        self.log_file = log_file

    @property
    def is_running(self):
        if self.process is None:
            return False
        ret = self.process.poll()
        return ret is None

    @property
    def should_abort(self):
        return self.restart_count >= MAX_RESTARTS

    def start(self):
        now = time.time()
        if self.last_start > 0:
            delay = min(
                RESTART_BASE_DELAY * (2 ** (self.restart_count - 1)),
                RESTART_MAX_DELAY,
            )
            elapsed = now - self.last_start
            if elapsed < delay:
                wait = delay - elapsed
                log.info("[%s] 等待 %.1f 秒后重启 (第 %d 次)...",
                         self.name, wait, self.restart_count)
                time.sleep(wait)

        self.last_start = time.time()
        self.restart_count += 1

        log.info("[%s] 启动命令: %s (第 %d 次尝试)",
                 self.name, " ".join(self.cmd), self.restart_count)

        stdout_target = None
        stderr_target = None
        if self.log_file:
            stdout_target = open(self.log_file, "a", encoding="utf-8")
            stderr_target = subprocess.STDOUT

        try:
            self.process = subprocess.Popen(
                self.cmd,
                cwd=self.workdir,
                env=self.env,
                stdout=stdout_target,
                stderr=stderr_target,
                creationflags=0,
            )
        except FileNotFoundError:
            log.error("[%s] 找不到可执行文件: %s", self.name, self.cmd[0])
            raise
        except Exception as exc:
            log.error("[%s] 启动失败: %s", self.name, exc)
            raise

    def stop(self):
        if self.process is None:
            return
        if not self.is_running:
            self.process = None
            return
        log.info("[%s] 正在关闭 (PID=%d)...", self.name, self.process.pid)
        try:
            self.process.terminate()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("[%s] 进程未响应，强制终止 (PID=%d)", self.name, self.process.pid)
            try:
                self.process.kill()
                self.process.wait(timeout=3)
            except OSError:
                pass
        if self.log_file and hasattr(self.process, 'stdout') and self.process.stdout:
            try:
                self.process.stdout.close()
            except OSError:
                pass
        self.process = None


class Watchdog:

    def __init__(self, monitor_bot=True, monitor_dashboard=True, verbose=False):
        self.processes = []
        self._shutdown = False
        self._shutdown_event = asyncio.Event()
        self.verbose = verbose

        python = sys.executable or "python"

        if monitor_dashboard:
            log_file = None if verbose else os.path.join(LOG_DIR, "dashboard.log")
            self.processes.append(ManagedProcess(
                name="Dashboard",
                cmd=[python, "-m", "dashboard.app"],
                log_file=log_file,
            ))

        if monitor_bot:
            log_file = None if verbose else os.path.join(LOG_DIR, "trading_bot.log")
            self.processes.append(ManagedProcess(
                name="TradingBot",
                cmd=[python, "main.py"],
                log_file=log_file,
            ))

        if not self.processes:
            log.error("没有需要监控的进程")
            sys.exit(1)

    def _print_banner(self):
        names = [p.name for p in self.processes]
        log.info("=" * 56)
        log.info("  自动交易看门狗 启动")
        log.info("  监控进程: %s", ", ".join(names))
        log.info("  启动顺序: Dashboard -> TradingBot (间隔 %ds)", STARTUP_DELAY)
        log.info("  最大重启次数: %d/进程", MAX_RESTARTS)
        log.info("  检查间隔: %ds", HEARTBEAT_INTERVAL)
        log.info("  日志文件: %s", LOG_FILE)
        if not self.verbose:
            log.info("  子进程日志: logs/trading_bot.log, logs/dashboard.log")
        log.info("  使用 --verbose 显示子进程日志到终端")
        log.info("=" * 56)

    async def start(self):
        self._print_banner()

        # 先启动 Dashboard
        dashboard_proc = None
        bot_proc = None
        for proc in self.processes:
            if proc.name == "Dashboard":
                dashboard_proc = proc
            if proc.name == "TradingBot":
                bot_proc = proc

        if dashboard_proc:
            try:
                dashboard_proc.start()
            except Exception as exc:
                log.error("[%s] 启动失败: %s", dashboard_proc.name, exc)

        # 等待几秒再启动 TradingBot
        if bot_proc:
            log.info("等待 %d 秒后启动 TradingBot...", STARTUP_DELAY)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=STARTUP_DELAY,
                )
                # 如果在等待期间收到关闭信号，直接返回
                return
            except asyncio.TimeoutError:
                pass
            try:
                bot_proc.start()
            except Exception as exc:
                log.error("[%s] 启动失败: %s", bot_proc.name, exc)

        # 注册关闭信号
        self._setup_signal_handlers()

        # 监控循环
        while not self._shutdown:
            all_dead = True
            for proc in self.processes:
                if proc.is_running:
                    all_dead = False
                elif proc.should_abort:
                    log.error("[%s] 已达最大重启次数，不再尝试", proc.name)
                else:
                    log.warning("[%s] 进程已退出，准备重启", proc.name)
                    try:
                        proc.start()
                    except Exception as exc:
                        log.error("[%s] 重启失败: %s", proc.name, exc)

            if all_dead and self._all_aborted:
                log.error("所有进程均已耗尽重启次数，看门狗退出")
                break

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=HEARTBEAT_INTERVAL,
                )
            except asyncio.TimeoutError:
                continue

    @property
    def _all_aborted(self):
        return all(p.should_abort for p in self.processes if not p.is_running)

    def _setup_signal_handlers(self):
        def _handler(signum, frame):
            self.stop()
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            pass

    def stop(self):
        if self._shutdown:
            return
        self._shutdown = True
        log.info("收到关闭信号，正在停止所有子进程...")
        for proc in reversed(self.processes):
            proc.stop()
        log.info("看门狗已退出")
        self._shutdown_event.set()


def parse_args():
    parser = argparse.ArgumentParser(description="自动交易机器人看门狗")
    parser.add_argument("--no-dashboard", action="store_true", help="不启动监控面板")
    parser.add_argument("--no-bot", action="store_true", help="不启动交易机器人")
    parser.add_argument("--verbose", action="store_true", help="子进程日志输出到终端")
    return parser.parse_args()


def main():
    args = parse_args()
    watchdog = Watchdog(
        monitor_bot=not args.no_bot,
        monitor_dashboard=not args.no_dashboard,
        verbose=args.verbose,
    )
    try:
        asyncio.run(watchdog.start())
    except KeyboardInterrupt:
        watchdog.stop()
    except Exception as exc:
        log.exception("看门狗异常: %s", exc)
        watchdog.stop()


if __name__ == "__main__":
    main()
