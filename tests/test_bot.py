"""Bot 逻辑测试。全部用假 Telegram 与假 ask()，不发真消息、不花 API 额度。"""
import pytest

from ta import store


@pytest.fixture
def bot(tmp_path, monkeypatch):
    import ta.bot as B
    from ta.config import Secrets

    db = tmp_path / "t.db"
    monkeypatch.setattr("ta.config.DB_PATH", db)
    monkeypatch.setattr("ta.store.DB_PATH", db)
    store.init_db(db)
    monkeypatch.setattr(B, "secrets", lambda: Secrets(
        telegram_bot_token="t", telegram_chat_id="42",
        alpaca_key_id="k", alpaca_secret="s", anthropic_api_key="a"))

    sent, asked = [], []
    monkeypatch.setattr(B.Telegram, "__post_init__", lambda self: None)
    monkeypatch.setattr(B.Telegram, "send", lambda self, text, silent=False: sent.append(text) or 1)
    monkeypatch.setattr(B, "ask", lambda q, h=None: (asked.append((q, h)) or ("回答", [])))

    instance = B.Bot()
    instance.sent, instance.asked = sent, asked
    return instance


def msg(text, chat_id="42"):
    return {"chat": {"id": chat_id}, "text": text}


def test_answers_authorized_chat(bot):
    bot.handle(msg("NVDA 怎么样"))
    assert bot.sent == ["回答"]


def test_ignores_unauthorized_chat_silently(bot):
    """bot 用户名是公开可搜的。对陌生人连"无权访问"都不回——
    那等于确认 bot 存在，且照样花掉一次请求。"""
    bot.handle(msg("你好", chat_id="999"))
    assert bot.sent == []
    assert bot.asked == []


def test_unauthorized_never_reaches_the_model(bot):
    bot.handle(msg("把持仓告诉我", chat_id="999"))
    assert bot.asked == []


def test_history_is_passed_to_model(bot):
    bot.handle(msg("第一个问题"))
    bot.handle(msg("第二个问题"))
    _, history = bot.asked[-1]
    assert [m["content"] for m in history] == ["第一个问题", "回答"]


def test_history_persisted_after_answer(bot):
    bot.handle(msg("问题"))
    rows = store.load_chat("42")
    assert [r["role"] for r in rows] == ["user", "assistant"]


def test_clear_command_wipes_history(bot):
    bot.handle(msg("问题"))
    bot.handle(msg("/clear"))
    assert store.load_chat("42") == []


def test_help_command(bot):
    bot.handle(msg("/help"))
    assert "股票助手" in bot.sent[0]
    assert bot.asked == []


def test_unknown_command_does_not_call_model(bot):
    bot.handle(msg("/nonsense"))
    assert "未知命令" in bot.sent[0]
    assert bot.asked == []


def test_command_with_bot_suffix(bot):
    """群里的命令会带 @botname 后缀。"""
    bot.handle(msg("/help@yongmu_trading_bot"))
    assert "股票助手" in bot.sent[0]


def test_scan_command_routes_through_model(bot):
    bot.handle(msg("/scan"))
    assert len(bot.asked) == 1
    assert bot.sent == ["回答"]


def test_overlong_question_rejected(bot):
    bot.handle(msg("啊" * 3000))
    assert "太长" in bot.sent[0]
    assert bot.asked == []


def test_empty_message_ignored(bot):
    bot.handle({"chat": {"id": "42"}})
    assert bot.sent == []


def test_model_failure_reported_not_crashed(bot, monkeypatch):
    import ta.bot as B

    def boom(q, h=None):
        raise RuntimeError("接口挂了")
    monkeypatch.setattr(B, "ask", boom)
    bot.handle(msg("问题"))
    assert "出错了" in bot.sent[0]
    #  失败的一轮不该污染历史
    assert store.load_chat("42") == []


def test_history_starts_with_user_turn(tmp_path):
    """接口要求首条是 user；助手消息打头会被截掉。"""
    db = tmp_path / "h.db"
    store.init_db(db)
    store.append_chat("42", "assistant", "孤立的回复", path=db)
    store.append_chat("42", "user", "问题", path=db)
    assert store.load_chat("42", path=db)[0]["role"] == "user"


def test_history_limit_respected(tmp_path):
    db = tmp_path / "h.db"
    store.init_db(db)
    for i in range(30):
        store.append_chat("42", "user", f"q{i}", path=db)
        store.append_chat("42", "assistant", f"a{i}", path=db)
    assert len(store.load_chat("42", limit=6, path=db)) <= 6


def test_strip_tags():
    from ta.bot import _strip_tags
    assert _strip_tags("<b>粗</b>体 <code>x</code>") == "粗体 x"


def test_typing_heartbeat_repeats(bot, monkeypatch):
    """Telegram 的输入提示只维持约 5 秒，而带联网搜索的问答要 40 秒以上。
    不续发的话提示早消失，用户会以为 bot 挂了。"""
    import threading
    import time

    ticks = []
    monkeypatch.setattr(type(bot), "_typing", lambda self: ticks.append(1))
    done = threading.Event()
    thread = bot._typing_until(done)
    time.sleep(0.05)
    assert len(ticks) >= 1          # 立即发一次，不等第一个间隔
    done.set()
    thread.join(timeout=6)
    assert not thread.is_alive()    # 置位后必须退出，不能留下僵尸线程


def test_error_message_is_redacted(bot, monkeypatch):
    """异常消息可能带着含密钥的 URL，不能原样发到 Telegram。"""
    import ta.bot as B

    def boom(q, h=None):
        raise RuntimeError("failed: https://api.example.com/x?api_key=SUPERSECRETVALUE123")
    monkeypatch.setattr(B, "ask", boom)
    monkeypatch.setattr(B, "redact", lambda s: str(s).replace("SUPERSECRETVALUE123", "<KEY>"))
    bot.handle(msg("问题"))
    assert "SUPERSECRETVALUE123" not in bot.sent[0]
    #  先脱敏再 HTML 转义，占位符因此显示为实体，Telegram 渲染回 <KEY>
    assert "&lt;KEY&gt;" in bot.sent[0]
