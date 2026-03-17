"""
通知模組：支援 LINE Notify / Telegram
設定在 config.yaml notifications 區塊，或透過環境變數注入。
"""
import logging
import os
import threading
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger("notifier")


class Notifier:
    def __init__(self, config: dict):
        cfg = config.get("notifications", {})
        self._enabled: bool = cfg.get("enabled", False)
        self._line_token: Optional[str] = (
            cfg.get("line_token") or os.environ.get("LINE_NOTIFY_TOKEN")
        )
        tg = cfg.get("telegram", {})
        self._tg_token: Optional[str] = (
            tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
        )
        self._tg_chat_id: Optional[str] = (
            tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
        )

        has_channel = bool(self._line_token or (self._tg_token and self._tg_chat_id))
        if self._enabled and not has_channel:
            logger.warning("通知已啟用但未設定任何頻道（LINE / Telegram）")

    def notify(self, message: str):
        """非阻塞發送通知，失敗時只記錄 log 不拋例外"""
        if not self._enabled:
            return
        threading.Thread(target=self._send, args=(message,), daemon=True).start()

    def _send(self, message: str):
        if self._line_token:
            self._send_line(message)
        if self._tg_token and self._tg_chat_id:
            self._send_telegram(message)

    def _send_line(self, message: str):
        try:
            data = urllib.parse.urlencode({"message": f"\n{message}"}).encode()
            req = urllib.request.Request(
                "https://notify-api.line.me/api/notify",
                data=data,
                headers={"Authorization": f"Bearer {self._line_token}"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning(f"LINE 通知失敗: {e}")

    def _send_telegram(self, message: str):
        try:
            data = urllib.parse.urlencode({
                "chat_id": self._tg_chat_id,
                "text": message,
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self._tg_token}/sendMessage",
                data=data,
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning(f"Telegram 通知失敗: {e}")
