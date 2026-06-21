"""网页信号监听器 —— 轮询 yss-signal.com API，检测新币种信号。

用法：
    listener = WebSignalListener(email, password, on_signal_callback)
    await listener.start()
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import requests

from dashboard import signal_store as ss
from dashboard import event_log as el

logger = logging.getLogger(__name__)


class WebSignalListener:
    """通过轮询 yss-signal.com API 跟踪币种报警次数，每次新报警触发分析。

    流程：
    1. 登录获取 Bearer Token
    2. 首次拉取全部信号列表，保存到 signals.json，记录每个币种的当前 alert_count（不触发交易）
    3. 每 15 分钟轮询一次，对比 alert_count 是否有增长
    4. alert_count 增长时保存到 signals.json 并回调 on_signal(symbol, entry_price, alert_count)
    5. Token 失效时自动重新登录
    """

    def __init__(self, email: str, password: str, on_signal_callback):
        self._email = email
        self._password = password
        self._on_signal = on_signal_callback
        self._token: str | None = None
        # { symbol: last_alert_count } —— 跟踪每只币最后处理的报警次数
        self._alert_tracker: dict[str, int] = {}
        self._running = False
        self._initial_loaded = False

    async def _sleep_until_next_poll(self):
        now = datetime.now()
        next_min = ((now.minute // 15) + 1) * 15
        next_hour = now.hour
        if next_min >= 60:
            next_min = 0
            next_hour += 1
        next_time = now.replace(hour=next_hour % 24, minute=next_min, second=30, microsecond=0)
        if next_time <= now:
            next_time += timedelta(days=1)
        delay = (next_time - now).total_seconds() + 30
        await asyncio.sleep(delay)

    async def start(self):
        """启动轮询循环。"""
        self._running = True

        retry = 0
        while not await self._login():
            retry += 1
            if retry >= 10:
                logger.error("YSS 多次登录失败，停止")
                return
            delay = min(30 * retry, 300)
            logger.error("YSS 登录失败，%d秒后重试...", delay)
            await asyncio.sleep(delay)

        logger.info("首次加载信号列表，保存到 signals.json ...")
        await self._fetch_and_update(initial_load=True)
        self._initial_loaded = True
        logger.info("初始化完成，已记录 %d 个币种", len(self._alert_tracker))

        while self._running:
            try:
                await self._fetch_and_update(trigger_new=True)
            except Exception as exc:
                logger.exception("轮询异常: %s", exc)
                if "401" in str(exc):
                    logger.info("Token 可能已失效，重新登录 ...")
                    if not await self._login():
                        logger.error("重新登录失败，停止轮询")
                        break
            await self._sleep_until_next_poll()

    async def stop(self):
        """停止轮询。"""
        self._running = False

    async def _login(self) -> bool:
        """用邮箱+密码登录，获取 Bearer Token。"""
        try:
            loop = asyncio.get_event_loop()
            def _sync():
                resp = requests.post(
                    "https://ai.yss-signal.com/login",
                    json={"email": self._email, "password": self._password},
                    timeout=15,
                )
                data = resp.json()
                if data.get("success"):
                    return data["token"]
                return None
            token = await loop.run_in_executor(None, _sync)
            if token:
                self._token = token
                logger.info("YSS 登录成功")
                return True
            logger.error("YSS 登录失败")
            return False
        except Exception as exc:
            logger.error("YSS 登录网络异常: %s", exc)
            return False

    async def _fetch_and_update(self, trigger_new: bool = False, initial_load: bool = False) -> None:
        """拉取信号列表，检测 alert_count 增长，更新跟踪记录。"""
        loop = asyncio.get_event_loop()
        def _sync():
            resp = requests.get(
                f"https://ai.yss-signal.com/?data=1&t={int(time.time() * 1000)}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=15,
            )
            if resp.status_code == 401:
                raise Exception("401 UNAUTHORIZED")
            return resp.json()
        data = await loop.run_in_executor(None, _sync)

        signals = data.get("signals", [])

        # 首次加载：全部保存到 signals.json
        if initial_load:
            batch = []
            for s in signals:
                symbol = s.get("symbol", "").upper().strip()
                if not symbol:
                    continue
                alert_count = int(s.get("alert_count", 0) or 0)
                entry_price = s.get("first_price") or s.get("price") or 0
                direction = s.get("direction", "LONG")
                score = s.get("score", 75)
                sig = {
                    "symbol": symbol + "USDT",
                    "direction": direction,
                    "price": float(entry_price),
                    "score": score,
                    "alert_count": alert_count,
                    "strategy": "YSS",
                    "status": "ACTIVE",
                    "trade_status": "待定",
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
                self._alert_tracker[symbol] = alert_count
                batch.append(sig)
            ss.save_all(batch)
            logger.info("首次加载: 保存 %d 条信号到 signals.json", len(batch))
            el.write("信号", "SYSTEM", "首次加载 " + str(len(batch)) + " 条信号")
            return

        # 常规轮询：检测报警增长
        for s in signals:
            symbol = s.get("symbol", "").upper().strip()
            if not symbol:
                continue

            entry_price = s.get("first_price") or s.get("price") or 0
            direction = s.get("direction", "LONG")
            score = s.get("score", 75)
            alert_count = int(s.get("alert_count", 0) or 0)
            last_count = self._alert_tracker.get(symbol, 0)

            if trigger_new and alert_count > last_count:
                logger.info("检测到新报警: %s 第%d次报警 入场价=%.6f", symbol, alert_count, float(entry_price))

                # 保存到 signals.json
                sig = {
                    "symbol": symbol + "USDT",
                    "direction": direction,
                    "price": float(entry_price),
                    "score": score,
                    "alert_count": alert_count,
                    "strategy": "YSS",
                    "status": "ACTIVE",
                    "trade_status": "待定",
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
                ss.add_signal(sig)
                # 触发交易分析
                await self._on_signal(symbol, float(entry_price), alert_count)

            self._alert_tracker[symbol] = alert_count

        # 移除已不在信号列表中的币种（释放内存）
        active_symbols = {s.get("symbol", "").upper().strip() for s in signals if s.get("symbol")}
        for sym in list(self._alert_tracker.keys()):
            if sym not in active_symbols:
                del self._alert_tracker[sym]

