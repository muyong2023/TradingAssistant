"""Telegram 层测试：分段、重试、错误处理，全部用假 transport，不发真消息。"""
import pytest
import requests

from ta.notify.telegram import Telegram, TelegramError, escape, split_message


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """记录每次调用；responses 用完后一律返回 200。"""
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "data": data, "files": files})
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return FakeResponse()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("ta.notify.telegram.time.sleep", lambda s: None)


def make(session):
    return Telegram(token="t", chat_id="1", session=session)


def test_split_short_message_untouched():
    assert split_message("hello") == ["hello"]


def test_split_respects_line_boundaries():
    lines = [f"line-{i}" for i in range(100)]
    parts = split_message("\n".join(lines), limit=100)
    assert len(parts) > 1
    # 没有任何一行被切断
    rejoined = "\n".join(parts).split("\n")
    assert rejoined == lines


def test_split_every_part_within_limit():
    text = "\n".join("x" * 50 for _ in range(200))
    for part in split_message(text, limit=100):
        assert len(part) <= 100


def test_split_handles_single_overlong_line():
    parts = split_message("y" * 250, limit=100)
    assert len(parts) == 3
    assert "".join(parts) == "y" * 250


def test_send_splits_into_multiple_calls():
    s = FakeSession()
    n = make(s).send("\n".join("z" * 500 for _ in range(20)))
    assert n == len(s.calls) > 1


def test_send_uses_html_parse_mode():
    s = FakeSession()
    make(s).send("hi")
    assert s.calls[0]["data"]["parse_mode"] == "HTML"


def test_retries_on_429_then_succeeds():
    s = FakeSession([FakeResponse(429, {"parameters": {"retry_after": 1}}), FakeResponse()])
    make(s).send("hi")
    assert len(s.calls) == 2


def test_retries_on_5xx():
    s = FakeSession([FakeResponse(503), FakeResponse()])
    make(s).send("hi")
    assert len(s.calls) == 2


def test_does_not_retry_on_400():
    """400 是请求本身有问题，重试只会浪费时间。"""
    s = FakeSession([FakeResponse(400, text="bad request")])
    with pytest.raises(TelegramError):
        make(s).send("hi")
    assert len(s.calls) == 1


def test_retries_on_network_error():
    s = FakeSession([requests.ConnectionError("boom"), FakeResponse()])
    make(s).send("hi")
    assert len(s.calls) == 2


def test_gives_up_after_max_retries():
    s = FakeSession([FakeResponse(503)] * 10)
    with pytest.raises(TelegramError):
        make(s).send("hi")


def test_escape_html_entities():
    assert escape("Procter & Gamble <test>") == "Procter &amp; Gamble &lt;test&gt;"


def test_send_parts_returns_message_ids():
    s = FakeSession([FakeResponse(payload={"ok": True, "result": {"message_id": 77}})])
    assert make(s).send_parts("hi") == [77]


def test_edit_calls_edit_endpoint():
    s = FakeSession()
    assert make(s).edit(77, "新内容") is True
    assert s.calls[0]["url"].endswith("editMessageText")
    assert s.calls[0]["data"]["message_id"] == 77


def test_edit_treats_not_modified_as_success():
    """内容没变时 Telegram 返回 400 not modified，这不是故障。"""
    s = FakeSession([FakeResponse(400, text="Bad Request: message is not modified")])
    assert make(s).edit(77, "一样的内容") is True


def test_edit_returns_false_on_real_error():
    s = FakeSession([FakeResponse(400, text="Bad Request: message to edit not found")])
    assert make(s).edit(77, "x") is False


def test_delete_swallows_errors():
    s = FakeSession([FakeResponse(400, text="message can't be deleted")])
    make(s).delete(77)          # 不应抛出
