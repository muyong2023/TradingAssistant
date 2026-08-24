"""Telegram 推送。

三个必须处理好的点：
1. 单条消息上限 4096 字符，晨报会超 —— 按行边界分段，不切断表格行。
2. 429 限流要按服务端给的 retry_after 退避，不能死循环重试。
3. 用 HTML 而非 MarkdownV2：后者要求转义十几个字符，
   而股价、百分比、财报数字里到处是 . - ( ) ! 极易发送失败。
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass

import requests

from ta.config import secrets

API = "https://api.telegram.org"
LIMIT = 4096
MAX_RETRIES = 4
TIMEOUT = 30

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def escape(text: str) -> str:
    """转义用户/接口数据中的 HTML 特殊字符（公司名里的 & 很常见）。"""
    return html.escape(text, quote=False)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """按行边界切分，尽量不破坏排版；超长单行才硬切。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:            # 极端情况：单行就超限
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}{line}\n"
        if len(candidate) > limit:
            chunks.append(current.rstrip("\n"))
            current = f"{line}\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


@dataclass
class Telegram:
    token: str = ""
    chat_id: str = ""
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if not self.token or not self.chat_id:
            s = secrets()
            s.require("telegram_bot_token", "telegram_chat_id")
            self.token = self.token or s.telegram_bot_token
            self.chat_id = self.chat_id or s.telegram_chat_id
        self.session = self.session or requests.Session()

    def _post(self, method: str, data: dict, files: dict | None = None) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        delay = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, data=data, files=files, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise TelegramError(f"{method} 网络失败: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                # 服务端明确告知等多久，照做即可，不要指数退避瞎猜
                wait = _retry_after(resp)
                log.warning("Telegram 限流，%s 秒后重试", wait)
                if attempt == MAX_RETRIES:
                    raise TelegramError(f"{method} 持续限流")
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise TelegramError(f"{method} 服务端 {resp.status_code}")
                time.sleep(delay)
                delay *= 2
                continue

            # 4xx（除 429）是请求本身有问题，重试没有意义
            raise TelegramError(f"{method} 返回 {resp.status_code}: {resp.text[:300]}")
        raise TelegramError(f"{method} 重试耗尽")

    def send(self, text: str, silent: bool = False) -> int:
        """发送文本，自动分段。返回实际发出的消息条数。"""
        parts = split_message(text)
        for i, part in enumerate(parts):
            self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            })
            if i < len(parts) - 1:
                time.sleep(0.4)      # 连发时给限流留余量
        return len(parts)

    def send_photo(self, image: bytes, caption: str = "", filename: str = "chart.png") -> None:
        self._post(
            "sendPhoto",
            {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"photo": (filename, image, "image/png")},
        )


def _retry_after(resp: requests.Response) -> float:
    try:
        return float(resp.json().get("parameters", {}).get("retry_after", 5))
    except Exception:
        return 5.0
