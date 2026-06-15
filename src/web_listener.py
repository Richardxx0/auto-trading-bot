"""
网页信号监听器 —— 轮询 yss-signal.com API，检测新币种信号。

用法：
    listener = WebSignalListener(email, password, on_signal_callback)
    await listener.start()
"""
import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)


class WebSignalListener:
    """通过轮询 yss-signal.com API 跟踪币种报警次数，每次新报警触发分析。

    流程：
    1. 登录获取 Bearer Token
    2. 首次拉取全部信号列表，记录每个币种的当前 alert_count（不触发交易）
    3. 每 30 秒轮询一次，对比 alert_count 是否有增长
    4. alert_count 增长时回调 on_signal(symbol, entry_price, alert_count)
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

    async def start(self):
        """启动轮询循环。"""
        self._running = True

        if not await self._login():
            logger.error("YSS 登录失败，无法启动")
            return

        logger.info("首次加载信号列表，记录当前报警次数 ...")
        await self._fetch_and_update()
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
            await asyncio.sleep(30)

    async def stop(self):
        """停止轮询。"""
        self._running = False

    async def _login(self) -> bool:
        """用邮箱+密码登录，获取 Bearer Token。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://ai.yss-signal.com/login",
                    json={"email": self._email, "password": self._password},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        self._token = data["token"]
                        logger.info("YSS 登录成功")
                        return True
                    logger.error("YSS 登录失败: %s", data.get("error"))
                    return False
        except Exception as exc:
            logger.error("YSS 登录网络异常: %s", exc)
            return False

    async def _fetch_and_update(self, trigger_new: bool = False) -> None:
        """拉取信号列表，检测 alert_count 增长，更新跟踪记录。"""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://ai.yss-signal.com/?data=1&t={int(time.time() * 1000)}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    raise Exception("401 UNAUTHORIZED")
                data = await resp.json()

        signals = data.get("signals", [])

        for s in signals:
            symbol = s.get("symbol", "").upper().strip()
            if not symbol:
                continue

            alert_count = int(s.get("alert_count", 0) or 0)
            last_count = self._alert_tracker.get(symbol, 0)

            if trigger_new and alert_count > last_count:
                entry_price = s.get("first_price") or s.get("price") or 0
                logger.info(
                    "检测到新报警: %s 第%d次报警 入场价=%.6f",
                    symbol, alert_count, float(entry_price),
                )
                await self._on_signal(symbol, float(entry_price), alert_count)

            self._alert_tracker[symbol] = alert_count

        # 移除已不在信号列表中的币种（释放内存）
        active_symbols = {s.get("symbol", "").upper().strip() for s in signals if s.get("symbol")}
        for sym in list(self._alert_tracker.keys()):
            if sym not in active_symbols:
                del self._alert_tracker[sym]
