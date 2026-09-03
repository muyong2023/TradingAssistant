"""自选股网页端测试：增删、校验、跨站防护。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, isolated_config):
    """让网页改的是测试用的配置副本，不碰真实文件。"""
    import ta.config as C
    #  校验默认放行，各用例按需覆盖
    monkeypatch.setattr("ta.watchlist.validate", lambda s: (True, "测试标的"))
    from ta.web.app import app
    return TestClient(app, follow_redirects=False)


def test_page_renders(client):
    r = client.get("/watchlist")
    assert r.status_code == 200
    assert "自选股" in r.text
    assert "NVDA" in r.text


def test_add_symbol(client):
    r = client.post("/watchlist/add", data={"symbol": "ORCL", "group": "growth_ai"})
    #  成功与失败都是 303，必须看重定向里带的是 msg 还是 err，
    #  否则一次静默失败也能让断言通过
    assert "msg=" in r.headers["location"], r.headers["location"]
    from ta.watchlist import groups
    assert "ORCL" in groups()["growth_ai"]


def test_add_normalizes_case(client):
    client.post("/watchlist/add", data={"symbol": "orcl", "group": "etf"})
    from ta.watchlist import groups
    assert "ORCL" in groups()["etf"]


def test_add_rejects_unknown_symbol(client, monkeypatch):
    """写错代码必须当场拦下——行情接口对不存在的代码只会静默返回空。"""
    monkeypatch.setattr("ta.watchlist.validate",
                        lambda s: (False, f"{s} 在交易所资产库里查不到"))
    r = client.post("/watchlist/add", data={"symbol": "APPL", "group": "core_mega"})
    assert "err=" in r.headers["location"]
    from ta.watchlist import groups
    assert "APPL" not in groups()["core_mega"]


def test_add_duplicate_reports_error(client):
    r = client.post("/watchlist/add", data={"symbol": "NVDA", "group": "etf"})
    assert "err=" in r.headers["location"]


def test_remove_symbol(client):
    r = client.post("/watchlist/remove", data={"symbol": "MU"})
    assert "msg=" in r.headers["location"], r.headers["location"]
    from ta.watchlist import groups
    assert "MU" not in groups()["core_mega"]


def test_remove_unknown_reports_error(client):
    r = client.post("/watchlist/remove", data={"symbol": "ZZZZ"})
    assert "err=" in r.headers["location"]


def test_cross_site_write_rejected(client):
    """页面无认证地监听在回环地址，浏览器允许任意站点向它提交表单。
    没有这道检查，别处打开的恶意页面就能悄悄改自选股。"""
    r = client.post("/watchlist/add",
                    data={"symbol": "TSLA", "group": "etf"},
                    headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_same_origin_write_allowed(client):
    r = client.post("/watchlist/add",
                    data={"symbol": "ORCL", "group": "etf"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 303


def test_no_origin_header_allowed(client):
    """命令行工具与部分浏览器不发 Origin，不能一刀切拒绝。"""
    assert client.post("/watchlist/add",
                       data={"symbol": "ORCL", "group": "etf"}).status_code == 303
