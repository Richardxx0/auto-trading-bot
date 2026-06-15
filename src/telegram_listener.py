"""
Telegram 频道消息监听器（基于 Telethon），支持自动重连。
"""
import asyncio
import logging
from typing import Callable, Awaitable

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    RPCError,
)
from telethon.network import ConnectionTcpFull

from config.settings import Config

logger = logging.getLogger(__name__)

MessageCallback = Callable[[str, str], Awaitable[None]]


class TelegramListener:
    """连接 Telegram 并监听新消息。

    特性：
    - 自动重连（断线或网络错误时按指数退避重试）
    - 可配置监听频道
    - 将 (发送者, 文本) 传递给回调函数
    """

    # 重连延迟（秒），指数退避
    _RECONNECT_DELAYS = [5, 15, 30, 60, 120]

    def __init__(self, config: Config, on_message: MessageCallback):
        self._cfg = config
        self._on_message = on_message
        self._client: TelegramClient | None = None
        self._running = False

    async def start(self) -> None:
        """连接并开始监听，断线时自动重连。"""
        self._running = True
        attempt = 0

        while self._running:
            try:
                await self._连接并监听()
            except KeyboardInterrupt:
                logger.info("收到退出信号")
                self._running = False
                break
            except (ConnectionError, OSError, RPCError) as exc:
                attempt += 1
                delay = self._RECONNECT_DELAYS[
                    min(attempt, len(self._RECONNECT_DELAYS)) - 1
                ]
                logger.warning(
                    "连接断开 (%s)。%d 秒后重试（第 %d 次）...",
                    exc, delay, attempt,
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                logger.exception("监听器未知错误: %s", exc)
                await asyncio.sleep(30)

    async def stop(self) -> None:
        """断开 Telegram 连接。"""
        self._running = False
        if self._client:
            await self._client.disconnect()
            logger.info("Telegram 客户端已断开")

    # ── 内部方法 ──────────────────────────────────────────────

    async def _连接并监听(self) -> None:
        logger.info("正在构建 Telegram 客户端 ...")
        self._client = TelegramClient(
            session="codex_telegram_session",
            api_id=self._cfg.telegram_api_id,
            api_hash=self._cfg.telegram_api_hash,
            connection=ConnectionTcpFull,
            request_retries=5,        # 短暂网络抖动时自动重试
            flood_sleep_threshold=60,
        )

        await self._client.start(phone=self._cfg.telegram_phone)
        me = await self._client.get_me()
        logger.info("Telegram 客户端启动成功（用户: @%s）", me.username or me.id)

        # 注册消息处理器
        target = (
            self._cfg.telegram_channel.strip()
            if self._cfg.telegram_channel
            else None
        )

        if target:
            logger.info("正在监听频道: %s", target)

            @self._client.on(events.NewMessage(chats=target))
            async def handler(event):
                await self._派发(event)
        else:
            logger.info("未指定频道，将监听所有对话")

            @self._client.on(events.NewMessage)
            async def handler(event):
                await self._派发(event)

        logger.info("Telegram 监听器已启动，等待消息中 ...")
        await self._client.run_until_disconnected()

    async def _派发(self, event) -> None:
        """将 Telethon 事件转换为 (发送者, 文本) 并回调。"""
        try:
            sender = await event.get_sender()
            sender_name = sender.username or sender.first_name or str(sender.id)
            text = event.raw_text or ""
            if not text:
                return
            logger.debug("收到新消息 来自 %s: %s", sender_name, text[:80])
            await self._on_message(sender_name, text)
        except Exception as exc:
            logger.error("消息派发失败: %s", exc)

    @property
    def is_connected(self) -> bool:
        """判断 Telegram 客户端是否已连接。"""
        return self._client is not None and self._client.is_connected()
