"""财报日程测试。"""
import json
from datetime import date, timedelta

import pytest

from ta import earnings as E
from ta.earnings import EarningsEvent


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "earnings.json"
    monkeypatch.setattr(E, "CACHE_PATH", path)
    return path


def ev(sym, day, end=None, eps=1.5, rev=2e10):
    return EarningsEvent(symbol=sym, day=day, day_end=end,
                         eps_estimate=eps, revenue_estimate=rev)


def test_single_date_is_confirmed():
    assert ev("NVDA", date(2026, 8, 26)).confirmed is True


def test_date_range_is_unconfirmed():
    """yfinance 返回两个日期表示公司尚未公告具体日期。"""
    e = ev("XYZ", date(2026, 9, 1), date(2026, 9, 5))
    assert e.confirmed is False
    assert "日期未定" in e.label()


def test_label_includes_estimates():
    label = ev("NVDA", date(2026, 8, 26), eps=2.09, rev=92.2e9).label()
    assert "NVDA" in label and "2.09" in label and "92.2B" in label


def test_label_without_estimates():
    label = EarningsEvent("XYZ", date(2026, 8, 26)).label()
    assert label == "XYZ"


def test_cache_roundtrip(cache):
    events = [ev("NVDA", date(2026, 8, 26)), ev("MU", date(2026, 9, 23))]
    E._write_cache(events)
    restored = E._read_cache()
    assert [e.symbol for e in restored] == ["NVDA", "MU"]
    assert restored[0].day == date(2026, 8, 26)


def test_stale_cache_ignored(cache):
    E._write_cache([ev("NVDA", date(2026, 8, 26))])
    raw = json.loads(cache.read_text())
    raw["fetched"] = (date.today() - timedelta(days=5)).isoformat()
    cache.write_text(json.dumps(raw))
    assert E._read_cache() is None


def test_corrupt_cache_ignored(cache):
    cache.write_text("{ 坏掉的 json")
    assert E._read_cache() is None


def test_all_events_falls_back_to_stale_cache(cache, monkeypatch):
    """全部抓取失败时沿用过期缓存，好过完全没有。"""
    E._write_cache([ev("NVDA", date(2026, 8, 26))])
    raw = json.loads(cache.read_text())
    raw["fetched"] = (date.today() - timedelta(days=9)).isoformat()
    cache.write_text(json.dumps(raw))
    monkeypatch.setattr(E, "fetch_all", lambda syms: [])
    assert [e.symbol for e in E.all_events(["NVDA"])] == ["NVDA"]


def test_upcoming_filters_and_sorts(cache, monkeypatch):
    events = [ev("MU", date(2026, 9, 23)), ev("NVDA", date(2026, 8, 26)),
              ev("KO", date(2026, 12, 1))]
    monkeypatch.setattr(E, "fetch_all", lambda syms: events)
    out = E.upcoming(["MU", "NVDA", "KO"], days=40, today=date(2026, 8, 25))
    assert [e.symbol for e in out] == ["NVDA", "MU"]      # KO 超出窗口


def test_fetch_all_skips_failing_symbol(monkeypatch):
    """单只失败不影响其余 —— ETF 和新上市标的常缺这项数据。"""
    def one(sym):
        if sym == "BAD":
            raise RuntimeError("404")
        return ev(sym, date(2026, 9, 1))
    monkeypatch.setattr(E, "_fetch_one", one)
    assert [e.symbol for e in E.fetch_all(["NVDA", "BAD", "MU"])] == ["NVDA", "MU"]
