"""Telegram 聊天 bot：长轮询接收消息，交给 Claude 回答。

    python -m ta.bot

用长轮询而非 webhook：这台 Mac 在 NAT 后面没有公网地址，
webhook 需要域名和端口转发，长轮询不需要任何暴露。

**只响应 config/.env 里配置的那个 chat_id。** bot 的用户名是公开可搜的，
任何人都能找到它并发消息；不加这道门，别人的提问会花你的 Anthropic 额度，
还能通过工具读到你的持仓。
"""
from __future__ import annotations

import argparse
import html
import logging
import signal
import sys
import threading
import time

import requests

from ta import store
from ta.chat import ask
from ta.config import config, redact, secrets
from ta.market import is_market_hours, now_et
from ta.notify.telegram import Telegram, TelegramError

log = logging.getLogger("ta.bot")

API = "https://api.telegram.org"
POLL_TIMEOUT = 50          # 长轮询挂起秒数
HTTP_TIMEOUT = POLL_TIMEOUT + 15
OFFSET_KEY = "telegram_offset"
MAX_QUESTION = 2000

HELP = """<b>股票助手</b>

直接提问即可，例如：
· NVDA 现在什么情况
· 我的持仓今天怎么样
· 哪只票 RSI 到超买了
· 这周有什么宏观数据
· MU 财报什么时候

<b>命令</b>
/scan — 关注列表当日概览
/news — 最近的相关新闻
/calendar — 宏观日程与财报
/clear — 清空对话上下文
/help — 显示本说明

回答里的数字都来自实时接口，不是模型的记忆。
本助手只做分析，不构成投资建议。"""


class Bot:
    def __init__(self) -> None:
        s = secrets()
        s.require("telegram_bot_token", "telegram_chat_id", "anthropic_api_key")
        self.token = s.telegram_bot_token
        self.allowed = str(s.telegram_chat_id)
        self.tg = Telegram(token=self.token, chat_id=self.allowed)
        self.session = requests.Session()
        self.running = True
        self.history_turns = int(config().get("chat", {}).get("history_turns", 12))

    # ---- Telegram 原语 ----

    def _get_updates(self, offset: int) -> list[dict]:
        try:
            resp = self.session.get(
                f"{API}/bot{self.token}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT,
                        "allowed_updates": '["message"]'},
                timeout=HTTP_TIMEOUT,
            )
        except requests.Timeout:
            return []          # 长轮询超时是正常的，不是错误
        except requests.RequestException as exc:
            log.warning("getUpdates 失败：%s", exc)
            time.sleep(5)
            return []
        if resp.status_code != 200:
            log.warning("getUpdates 返回 %s：%s", resp.status_code, resp.text[:200])
            time.sleep(5)
            return []
        payload = resp.json()
        return payload.get("result", []) if payload.get("ok") else []

    def _typing(self) -> None:
        try:
            self.session.post(f"{API}/bot{self.token}/sendChatAction",
                              data={"chat_id": self.allowed, "action": "typing"},
                              timeout=10)
        except requests.RequestException:
            pass

    def _typing_until(self, done: threading.Event) -> threading.Thread:
        """持续发送"正在输入"，直到 done 被设置。

        Telegram 的输入提示只维持约 5 秒，而带联网搜索的一轮问答实测
        要 40 秒以上。不续发的话提示早就消失，用户会以为 bot 挂了。
        """
        def loop() -> None:
            while not done.wait(4.0):
                self._typing()

        thread = threading.Thread(target=loop, daemon=True)
        self._typing()
        thread.start()
        return thread

    def _reply(self, text: str) -> None:
        try:
            self.tg.send(text)
        except TelegramError as exc:
            log.error("回复发送失败：%s", exc)
            #  多半是 HTML 语法不合法被 Telegram 拒收，退回纯文本重发一次
            try:
                self.tg.send(html.escape(_strip_tags(text)))
            except TelegramError:
                log.error("纯文本重发也失败了")

    # ---- 消息处理 ----

    def handle(self, message: dict) -> None:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text:
            return
        if chat_id != self.allowed:
            #  不回复陌生人，连"无权访问"都不回 —— 那等于确认 bot 活着
            log.warning("忽略来自未授权 chat_id %s 的消息", chat_id)
            return

        if text.startswith("/"):
            self.handle_command(text)
            return

        if len(text) > MAX_QUESTION:
            self._reply(f"问题太长了（{len(text)} 字），请精简到 {MAX_QUESTION} 字以内。")
            return

        started = time.time()
        done = threading.Event()
        self._typing_until(done)
        try:
            history = store.load_chat(self.allowed, limit=self.history_turns)
            answer, _ = ask(text, history)
        except Exception as exc:
            log.exception("回答失败")
            self._reply(f"出错了：<code>{html.escape(redact(str(exc))[:300])}</code>")
            return
        finally:
            done.set()

        store.append_chat(self.allowed, "user", text)
        store.append_chat(self.allowed, "assistant", answer)
        log.info("已回复（耗时 %.1fs，问题 %d 字）", time.time() - started, len(text))
        self._reply(answer)

    def handle_command(self, text: str) -> None:
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "help"):
            self._reply(HELP)
        elif cmd == "clear":
            n = store.clear_chat(self.allowed)
            self._reply(f"已清空对话上下文（{n} 条）。")
        elif cmd in ("scan", "news", "calendar"):
            #  命令走同一条问答链路，保证与自由提问的口径一致
            prompt = {
                "scan": "给我关注列表的当日概览：涨跌分布、涨跌幅前列、有没有 RSI 极值。",
                "news": "最近 24 小时我关注的标的有什么重要新闻？",
                "calendar": "未来两周有哪些宏观数据发布和我持仓的财报？",
            }[cmd]
            done = threading.Event()
            self._typing_until(done)
            try:
                answer, _ = ask(prompt, [])
            except Exception as exc:
                log.exception("命令 %s 失败", cmd)
                self._reply(f"出错了：<code>{html.escape(redact(str(exc))[:300])}</code>")
                return
            finally:
                done.set()
            self._reply(answer)
        else:
            self._reply(f"未知命令 {html.escape(cmd)}。发 /help 看可用命令。")

    # ---- 主循环 ----

    def stop(self, *_args) -> None:
        log.info("收到停止信号，正在退出")
        self.running = False

    def run(self) -> int:
        store.init_db()
        offset = int(store.get_state(OFFSET_KEY, "0") or 0)
        log.info("bot 已启动，只响应 chat_id %s（offset=%d）", self.allowed, offset)
        while self.running:
            for update in self._get_updates(offset):
                offset = max(offset, update["update_id"] + 1)
                #  先推进 offset 再处理：处理中崩溃时不会反复重放同一条消息
                store.set_state(OFFSET_KEY, str(offset))
                message = update.get("message")
                if message:
                    try:
                        self.handle(message)
                    except Exception:
                        log.exception("处理消息时出错，继续运行")
        return 0


def _strip_tags(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ta.bot", description="Telegram 聊天 bot")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ask", help="不启动轮询，直接问一个问题（用于测试）")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    #  SDK 会把每次 HTTP 请求打成 INFO，一次问答就是三四行噪音
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.ask:
        answer, _ = ask(args.ask, [])
        print(answer)
        return 0

    bot = Bot()
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
