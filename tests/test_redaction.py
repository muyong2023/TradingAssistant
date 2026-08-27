"""凭据脱敏测试。

背景：FRED 把 api_key 放在查询参数里，requests 的 raise_for_status()
会把整条 URL 写进异常消息，直接 log 出来就是明文落盘。
这个问题真实发生过 —— key 进了 logs/premarket.err.log 并被 git 跟踪。
"""
import pytest

from ta.config import Secrets, redact


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    fake = Secrets(
        telegram_bot_token="8123456789:AAH1a2B3c4D5e6F7g8H9i0J",
        telegram_chat_id="1234567890",
        alpaca_key_id="PKABCDEFGHIJKLMNOPQR",
        alpaca_secret="secret1234567890abcdefghij",
        anthropic_api_key="sk-ant-api03-xxxxxxxxxxxxxxxxxxxx",
        fred_api_key="0123456789abcdef0123456789abcdef",
    )
    monkeypatch.setattr("ta.config.secrets", lambda: fake)
    return fake


def test_redacts_key_in_error_url(fake_secrets):
    text = (f"502 Server Error for url: https://api.stlouisfed.org/fred/"
            f"release/dates?release_id=10&api_key={fake_secrets.fred_api_key}")
    out = redact(text)
    assert fake_secrets.fred_api_key not in out
    assert "<FRED_API_KEY>" in out


def test_redacts_telegram_token_in_url(fake_secrets):
    out = redact(f"https://api.telegram.org/bot{fake_secrets.telegram_bot_token}/sendMessage")
    assert fake_secrets.telegram_bot_token not in out


def test_redacts_every_configured_credential(fake_secrets):
    blob = " ".join(v for v in fake_secrets.__dict__.values() if v)
    out = redact(blob)
    for value in fake_secrets.__dict__.values():
        if value and len(value) > 8:
            assert value not in out


def test_accepts_non_string_input(fake_secrets):
    exc = RuntimeError(f"failed with key={fake_secrets.fred_api_key}")
    assert fake_secrets.fred_api_key not in redact(exc)


def test_leaves_unrelated_text_untouched():
    assert redact("普通日志，没有密钥") == "普通日志，没有密钥"


def test_ignores_short_values(monkeypatch):
    """太短的值不做替换，否则会把正常文本打得千疮百孔。"""
    monkeypatch.setattr("ta.config.secrets", lambda: Secrets(
        telegram_bot_token="", telegram_chat_id="42", alpaca_key_id="",
        alpaca_secret="", anthropic_api_key="", fred_api_key=""))
    assert redact("答案是 42") == "答案是 42"


def test_empty_credentials_are_noop(monkeypatch):
    monkeypatch.setattr("ta.config.secrets", lambda: Secrets("", "", "", "", "", ""))
    assert redact("任何文本") == "任何文本"
