"""YSS 信号轮询器 —— 每隔 30s 拉取一次最新信号，保存到 signals.json。"""

import logging
import os
import threading
import time
from datetime import datetime

import requests

from dashboard import signal_store as ss
from dashboard import event_log as el

logger = logging.getLogger(__name__)


class YssScraper:
    """轮询 yss-signal.com API 检测新币种信号并保存。"""

    def __init__(self):
        self._email = os.environ.get("YSS_EMAIL", "")
        self._password = os.environ.get("YSS_PASSWORD", "")
        self._token = None
        self._alert_tracker = {}
        self._running = False
        self._thread = None

    def start(self):
        if not self._email or not self._password:
            logger.warning("YSS 未配置，跳过信号轮询")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("YSS 信号轮询器已启动")

    def stop(self):
        self._running = False

    def _run(self):
        if not self._login():
            return
        # 首次加载，只记录不触发
        self._fetch_and_update()
        logger.info("YSS 初始化完成，记录 %d 个币种", len(self._alert_tracker))

        while self._running:
            try:
                self._fetch_and_update(trigger_new=True)
            except Exception as exc:
                logger.error("YSS 轮询异常: %s", exc)
                if "401" in str(exc):
                    logger.info("Token 失效，重新登录...")
                    if not self._login():
                        break
            time.sleep(900)

    def _login(self) -> bool:
        try:
            resp = requests.post(
                "https://ai.yss-signal.com/login",
                json={"email": self._email, "password": self._password},
                timeout=15,
            )
            data = resp.json()
            if data.get("success"):
                self._token = data["token"]
                logger.info("YSS 登录成功")
                return True
            logger.error("YSS 登录失败: %s", data.get("error"))
            return False
        except Exception as exc:
            logger.error("YSS 登录网络异常: %s", exc)
            return False

    def _fetch_and_update(self, trigger_new=False):
        resp = requests.get(
            f"https://ai.yss-signal.com/?data=1&t={int(time.time() * 1000)}",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            raise Exception("401 UNAUTHORIZED")
        data = resp.json()

        signals = data.get("signals", [])
        for s in signals:
            symbol = s.get("symbol", "").upper().strip()
            if not symbol:
                continue
            alert_count = int(s.get("alert_count", 0) or 0)
            last_count = self._alert_tracker.get(symbol, 0)

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
                    "trade_status": "pending",
                }
            if trigger_new and alert_count > last_count:
                    ss.add_signal(sig)
                    el.write("信号", symbol, "价格=" + str(entry_price)[:8] + " 第" + str(alert_count) + "次报警")
                    logger.info("新信号: %s %s @ %.4f (#%d)", symbol, direction, float(entry_price), alert_count)
            elif not trigger_new and alert_count > 0:
                    existing = ss.load_all()
                    exists = any(ex.get("symbol") == symbol + "USDT" and ex.get("alert_count") == alert_count for ex in existing)
                    if not exists:
                        ss.add_signal(sig)
                        el.write("信号", symbol, "初始加载 价格=" + str(entry_price)[:8])

            self._alert_tracker[symbol] = alert_count

        active = {s.get("symbol", "").upper().strip() for s in signals if s.get("symbol")}
        for sym in list(self._alert_tracker.keys()):
            if sym not in active:
                del self._alert_tracker[sym]
