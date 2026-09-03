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

from ta import store, watchlist
from ta.chat import ask
from ta.watchlist import WatchlistError
from ta.config import config, redact, secrets
from ta.market import is_market_hours, now_et
from ta.notify.telegram import (Telegram, TelegramError, escape,
                                split_message)

log = logging.getLogger("ta.bot")

API = "https://api.telegram.org"
POLL_TIMEOUT = 50          # 长轮询挂起秒数
HTTP_TIMEOUT = POLL_TIMEOUT + 15
OFFSET_KEY = "telegram_offset"
MAX_QUESTION = 2000
WAIT_ICON = "⏳"

HELP_CORE = """<b>股票助手</b>

<b>自选股</b>
/list — 查看全部分组与标的
/add &lt;代码&gt; [分组] — 加入，省略分组则进 {default_group}
/remove &lt;代码&gt; — 移除

<b>分组</b>
/newlist &lt;标识&gt; [显示名] [低 高] — 新建分组
/dellist &lt;标识&gt; [force] — 删除分组

<b>信号</b>
每 5 分钟检查一次，日线与 5 分钟线的 RSI
低于 {low} 或高于 {high} 时推送，两者分开成条。

/check — 立即跑一次 RSI 检查，有无信号都回报
/status — 当前配置与运行状态
/help — 显示本说明"""

HELP_CHAT = """

<b>问答</b>
直接提问即可，例如「NVDA 现在什么情况」。
/scan /news /calendar /clear"""


def build_help() -> str:
    cfg = config()
    rsi_cfg = cfg["indicators"]["rsi"]
    groups = list(cfg["watchlists"])
    text = HELP_CORE.format(low=rsi_cfg["oversold"], high=rsi_cfg["overbought"],
                            default_group=groups[0] if groups else "core")
    if cfg.get("chat", {}).get("enabled", True):
        text += HELP_CHAT
    return text


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

    def _progress_reporter(self):
        """返回（回调函数, 占位消息 id 容器）。

        先发一条占位消息，随模型调用工具实时改写它，最后由调用方
        把它就地改成答案。比只靠"正在输入"提示强得多——那个提示在
        顶栏一闪而过，用户看不出它在查什么、还要多久。
        """
        holder: dict = {"id": 0, "last": "", "steps": []}
        try:
            ids = self.tg.send_parts(f"{WAIT_ICON} 正在查…", silent=True)
            holder["id"] = ids[0] if ids else 0
        except TelegramError as exc:
            log.debug("占位消息发送失败：%s", exc)

        def report(labels: list[str]) -> None:
            #  进度显示纯属装饰。它自己吞掉所有异常，而不是指望调用方
            #  包一层 try —— 任何情况下都不该因为它而丢掉答案。
            try:
                _report(labels)
            except Exception as exc:
                log.debug("更新进度失败：%s", exc)

        def _report(labels: list[str]) -> None:
            if not holder["id"]:
                return
            for label in labels:
                if label not in holder["steps"]:
                    holder["steps"].append(label)
            #  已完成的步骤打勾，当前这批显示为进行中
            done = holder["steps"][:-len(labels)] if len(holder["steps"]) > len(labels) else []
            lines = [f"✓ <s>{escape(s)}</s>" for s in done]
            lines += [f"{WAIT_ICON} {escape(s)}…" for s in labels]
            text = "\n".join(lines)
            if text != holder["last"]:
                holder["last"] = text
                self.tg.edit(holder["id"], text)

        return report, holder

    def _finish(self, holder: dict, answer: str) -> None:
        """把占位消息就地换成答案。"""
        placeholder = holder.get("id") or 0
        parts = split_message(answer)
        try:
            edited = bool(placeholder and parts and self.tg.edit(placeholder, parts[0]))
        except Exception as exc:
            #  编辑只是呈现方式，任何意外都不该让答案丢失
            log.debug("改写占位消息失败：%s", exc)
            edited = False
        if edited:
            for extra in parts[1:]:
                self.tg.send(extra)
            return
        #  编辑失败（消息被删、网络异常等）就删掉占位再正常发送
        if placeholder:
            try:
                self.tg.delete(placeholder)
            except Exception:
                pass
        self._reply(answer)

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

        if not config().get("chat", {}).get("enabled", True):
            #  问答是唯一花 token 的功能。关闭时直接短回复，绝不调模型。
            self._reply("问答功能当前已关闭（省 API 额度）。\n"
                        "可用命令：/list /add /remove /status /help")
            return

        if len(text) > MAX_QUESTION:
            self._reply(f"问题太长了（{len(text)} 字），请精简到 {MAX_QUESTION} 字以内。")
            return

        started = time.time()
        done = threading.Event()
        self._typing_until(done)
        report, holder = self._progress_reporter()
        try:
            history = store.load_chat(self.allowed, limit=self.history_turns)
            answer, _ = ask(text, history, on_progress=report)
        except Exception as exc:
            log.exception("回答失败")
            self._finish(holder, f"出错了：<code>{html.escape(redact(str(exc))[:300])}</code>")
            return
        finally:
            done.set()

        store.append_chat(self.allowed, "user", text)
        store.append_chat(self.allowed, "assistant", answer)
        log.info("已回复（耗时 %.1fs，%d 步）", time.time() - started, len(holder["steps"]))
        self._finish(holder, answer)

    def handle_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]

        #  以下命令全是本地逻辑，不调用任何模型，零 token 开销
        if cmd in ("start", "help"):
            self._reply(build_help())
        elif cmd == "list":
            self._reply(watchlist.summary())
        elif cmd == "add":
            self._cmd_add(args)
        elif cmd == "remove":
            self._cmd_remove(args)
        elif cmd == "newlist":
            self._cmd_newlist(args)
        elif cmd == "dellist":
            self._cmd_dellist(args)
        elif cmd == "check":
            self._cmd_check()
        elif cmd == "status":
            self._reply(self._status())
        elif cmd == "clear":
            n = store.clear_chat(self.allowed)
            self._reply(f"已清空对话上下文（{n} 条）。")
        elif not config().get("chat", {}).get("enabled", True):
            self._reply("问答功能当前已关闭（省 API 额度）。\n"
                        "可用命令见 /help；要开启改 <code>config.yaml</code> 的 "
                        "<code>chat.enabled</code>。")
        elif cmd in ("scan", "news", "calendar"):
            #  命令走同一条问答链路，保证与自由提问的口径一致
            prompt = {
                "scan": "给我关注列表的当日概览：涨跌分布、涨跌幅前列、有没有 RSI 极值。",
                "news": "最近 24 小时我关注的标的有什么重要新闻？",
                "calendar": "未来两周有哪些宏观数据发布和我持仓的财报？",
            }[cmd]
            done = threading.Event()
            self._typing_until(done)
            report, holder = self._progress_reporter()
            try:
                answer, _ = ask(prompt, [], on_progress=report)
            except Exception as exc:
                log.exception("命令 %s 失败", cmd)
                self._finish(holder, f"出错了：<code>{html.escape(redact(str(exc))[:300])}</code>")
                return
            finally:
                done.set()
            self._finish(holder, answer)
        else:
            self._reply(f"未知命令 {html.escape(cmd)}。发 /help 看可用命令。")

    def _cmd_add(self, args: list[str]) -> None:
        if not args:
            self._reply("用法：<code>/add NVDA [分组]</code>\n"
                        f"可用分组：{', '.join(config()['watchlists'])}")
            return
        groups = list(config()["watchlists"])
        group = args[1] if len(args) > 1 else groups[0]
        try:
            self._reply(watchlist.add(args[0], group))
        except WatchlistError as exc:
            self._reply(f"{html.escape(str(exc))}")

    def _cmd_remove(self, args: list[str]) -> None:
        if not args:
            self._reply("用法：<code>/remove NVDA</code>")
            return
        try:
            self._reply(watchlist.remove(args[0]))
        except WatchlistError as exc:
            self._reply(f"{html.escape(str(exc))}")

    def _cmd_newlist(self, args: list[str]) -> None:
        if not args:
            self._reply("用法：<code>/newlist 标识 [显示名] [低阈值 高阈值]</code>\n"
                        "例：<code>/newlist dividend 高股息 6 11</code>")
            return
        key = args[0]
        label = args[1] if len(args) > 1 else key
        pct = (float(args[2]), float(args[3])) if len(args) > 3 else watchlist.DEFAULT_PCT
        try:
            self._reply(watchlist.create_group(key, label, pct))
        except (WatchlistError, ValueError) as exc:
            self._reply(html.escape(str(exc)))

    def _cmd_dellist(self, args: list[str]) -> None:
        if not args:
            self._reply("用法：<code>/dellist 标识 [force]</code>\n"
                        "组内还有标的时需加 force 确认")
            return
        force = len(args) > 1 and args[1].lower() in ("force", "-f", "yes")
        try:
            self._reply(watchlist.delete_group(args[0], force=force))
        except WatchlistError as exc:
            self._reply(html.escape(str(exc)))

    def _cmd_check(self) -> None:
        """手动跑一次 RSI 检查，有无信号都回报。

        用 summary 模式：不占当日的去重额度，否则手动查一次
        会把后面真正的自动告警吞掉。
        """
        done = threading.Event()
        self._typing_until(done)
        try:
            from ta.jobs import job_intraday
            job_intraday(force=True, summary=True)
        except Exception as exc:
            log.exception("手动检查失败")
            self._reply(f"检查失败：<code>{html.escape(redact(str(exc))[:300])}</code>")
        finally:
            done.set()

    def _status(self) -> str:
        cfg = config()
        rsi_cfg = cfg["indicators"]["rsi"]
        intraday = cfg["indicators"].get("intraday", {})
        total = sum(len(g["symbols"]) for g in cfg["watchlists"].values())
        jobs = cfg.get("jobs", {})
        on = lambda v: "开" if v else "关"
        return (
            f"<b>运行状态</b>\n"
            f"自选股 {total} 只 / {len(cfg['watchlists'])} 组\n"
            f"RSI 阈值 {rsi_cfg['oversold']} / {rsi_cfg['overbought']}\n"
            f"日线信号 {on(cfg['alerts'].get('rsi_alert', True))}　"
            f"{intraday.get('label', '分钟')}线信号 {on(intraday.get('enabled'))}\n"
            f"涨跌幅告警 {on(cfg['alerts'].get('pct_move_alert'))}\n"
            f"盘中检查 {on(jobs.get('intraday', True))}　"
            f"晨报 {on(jobs.get('premarket'))}　盘后 {on(jobs.get('postclose'))}\n"
            f"问答 {on(cfg.get('chat', {}).get('enabled'))}"
        )

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
