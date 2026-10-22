"""通知渠道：Telegram / 企业微信机器人 / 通用 Webhook / 标准输出。

未配置任何渠道时只打日志；单渠道失败不影响其他渠道，也不会让任务失败。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

TG_API = "https://api.telegram.org/bot{token}/sendMessage"
TG_LIMIT = 3800


class Channel(ABC):
    name = "channel"

    def __init__(self, logger: Optional[Any] = None):
        self.log = logger

    @abstractmethod
    def send(self, title: str, text: str) -> bool:
        ...


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, thread_id: str = "", logger: Optional[Any] = None):
        super().__init__(logger)
        self.token = bot_token
        self.chat_id = chat_id
        self.thread_id = thread_id

    def send(self, title: str, text: str) -> bool:
        ok = True
        for chunk in _chunks(f"{title}\n\n{text}", TG_LIMIT):
            payload: Dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if self.thread_id:
                payload["message_thread_id"] = int(self.thread_id)
            try:
                r = requests.post(TG_API.format(token=self.token), json=payload, timeout=20)
                if r.status_code != 200:
                    ok = False
                    if self.log:
                        self.log.warning("Telegram 推送失败 %s: %s", r.status_code, r.text[:200])
            except Exception as e:
                ok = False
                if self.log:
                    self.log.warning("Telegram 推送异常: %s", e)
        return ok


class WecomChannel(Channel):
    name = "wecom"

    def __init__(self, webhook: str, logger: Optional[Any] = None):
        super().__init__(logger)
        self.webhook = webhook

    def send(self, title: str, text: str) -> bool:
        try:
            r = requests.post(self.webhook, json={
                "msgtype": "text",
                "text": {"content": f"{title}\n{text}"},
            }, timeout=20)
            return r.status_code == 200
        except Exception as e:
            if self.log:
                self.log.warning("企业微信推送异常: %s", e)
            return False


class WebhookChannel(Channel):
    name = "webhook"

    def __init__(self, url: str, logger: Optional[Any] = None):
        super().__init__(logger)
        self.url = url

    def send(self, title: str, text: str) -> bool:
        try:
            r = requests.post(self.url, json={"title": title, "text": text}, timeout=20)
            return 200 <= r.status_code < 300
        except Exception as e:
            if self.log:
                self.log.warning("Webhook 推送异常: %s", e)
            return False


class StdoutChannel(Channel):
    name = "stdout"

    def send(self, title: str, text: str) -> bool:
        print(f"\n===== {title} =====\n{text}\n")
        return True


def _chunks(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class Notifier:
    def __init__(self, cfg: Any, logger: Optional[Any] = None, always_stdout: bool = False):
        self.log = logger
        self.channels: List[Channel] = [StdoutChannel(logger)] if always_stdout else []
        tg = cfg.telegram or {}
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            self.channels.append(TelegramChannel(
                str(tg["bot_token"]), str(tg["chat_id"]), str(tg.get("thread_id") or ""), logger))
        wc = cfg.wecom or {}
        if wc.get("enabled") and wc.get("webhook"):
            self.channels.append(WecomChannel(str(wc["webhook"]), logger))
        wh = cfg.webhook or {}
        if wh.get("enabled") and wh.get("url"):
            self.channels.append(WebhookChannel(str(wh["url"]), logger))
        if not self.channels:
            self.channels.append(StdoutChannel(logger))

    def send(self, title: str, text: str) -> None:
        for ch in self.channels:
            try:
                ch.send(title, text)
            except Exception as e:
                if self.log:
                    self.log.warning("通知渠道 %s 异常: %s", ch.name, e)
