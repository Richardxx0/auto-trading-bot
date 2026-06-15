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
    """通过轮询 yss-signal.com API 检测新出现的币种信号。

    流程：
    1. 登录获取 Bearer Token
    2. 首次拉取全部信号列表，记入已见集合（不触发交易）
    3. 每 30 秒轮询一次，检测新币种
    4. 新币种出现时回调 on_signal(symbol, entry_price)
    5. Token 失效时自动重新登录
    """

    def __init__(self, email: str, password: str, on_signal_callback):
        self._email = email
        self._password = password
        self._on_signal = on_signal_callback
        self._token: str | None = None
        self._seen_symbols: set[str] = set()
        self._running = False

    async def start(self):
        """启动轮询循环。"""
        self._running = True

        if not await self._login():
            logger.error("YSS 登录失败，无法启动")
            return

        logger.info("首次加载信号列表，初始化已见币种 ...")
        await self._fetch_and_update()
        logger.info("初始化完成，已记录 %d 个币种", len(self._seen_symbols))

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
        """拉取信号列表，检测新币种，更新已见集合。"""
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
        current_symbols: set[str] = set()

        for s in signals:
            symbol = s.get("symbol", "").upper().strip()
            if not symbol:
                continue
            current_symbols.add(symbol)

            if trigger_new and symbol not in self._seen_symbols:
                entry_price = s.get("first_price") or s.get("price") or 0
                logger.info(
                    "检测到新币种信号: %s 入场价=%.6f",
                    symbol, float(entry_price),
                )
                await self._on_signal(symbol, float(entry_price))

        self._seen_symbols = current_symbols
