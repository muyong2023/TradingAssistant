"""经济日历测试。"""
from datetime import date, time

from ta.data.econ import EconEvent, _clean, _match, _parse_time


def test_match_core_releases():
    assert _match("CPI") == ("CPI", "通胀")
    assert _match("Core CPI") == ("CPI", "通胀")
    assert _match("Nonfarm Payrolls") == ("非农就业", "就业")
    assert _match("Initial Jobless Claims") == ("初请失业金", "就业")
    assert _match("ISM Manufacturing PMI") == ("ISM 制造业", "景气")


def test_match_ignores_unwanted():
    assert _match("3-Month Bill Auction") is None
    assert _match("Natural Gas Storage") is None
    assert _match("API Weekly Crude Oil Stock") is None


def test_match_excludes_cleveland_cpi_nowcast():
    """Cleveland CPI 是地方联储的预测值，不是官方 CPI 发布。"""
    assert _match("Cleveland CPI") is None


def test_clean_strips_markup():
    assert _clean("<a href='x'>CPI</a>") == "CPI"


def test_parse_time():
    assert _parse_time("08:30") == time(8, 30)
    assert _parse_time("") is None
    assert _parse_time("n/a") is None


def test_event_is_frozen():
    e = EconEvent(date(2026, 9, 11), "CPI", "通胀", time(8, 30))
    assert e.day == date(2026, 9, 11)


def test_subsumed_indicator_dropped(monkeypatch):
    """失业率与非农就业同出于 BLS 的就业报告，不必并列两条。"""
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "fetch_fred", lambda s, e: [
        EconEvent(date(2026, 9, 4), "非农就业", "就业", time(8, 30), "fred")])
    monkeypatch.setattr(econ, "fetch_nasdaq", lambda d: (
        [EconEvent(date(2026, 9, 4), "失业率", "就业", time(8, 30))]
        if d == date(2026, 9, 4) else []))
    out = econ.upcoming(date(2026, 9, 1), 7)
    assert [e.name for e in out] == ["非农就业"]


def test_subsumed_kept_when_primary_absent(monkeypatch):
    """主指标不在时，次要指标应当保留，否则会整天没有就业信息。"""
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "fetch_fred", lambda s, e: [])
    monkeypatch.setattr(econ, "fetch_nasdaq", lambda d: (
        [EconEvent(date(2026, 9, 4), "失业率", "就业", time(8, 30))]
        if d == date(2026, 9, 4) else []))
    assert [e.name for e in econ.upcoming(date(2026, 9, 1), 7)] == ["失业率"]


def test_fred_preferred_over_nasdaq_for_same_event(monkeypatch):
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "fetch_fred", lambda s, e: [
        EconEvent(date(2026, 9, 11), "CPI", "通胀", time(8, 30), "fred")])
    monkeypatch.setattr(econ, "fetch_nasdaq", lambda d: (
        [EconEvent(date(2026, 9, 11), "CPI", "通胀", time(8, 30), "nasdaq")]
        if d == date(2026, 9, 11) else []))
    out = econ.upcoming(date(2026, 9, 8), 7)
    assert len(out) == 1 and out[0].source == "fred"


def test_fred_returns_empty_without_key(monkeypatch):
    import ta.data.econ as econ
    from ta.config import Secrets
    monkeypatch.setattr(econ, "secrets", lambda: Secrets("", "", "", "", "", ""))
    assert econ.fetch_fred(date(2026, 9, 1), date(2026, 9, 30)) == []


def test_release_ids_are_documented_values():
    """id 写错会静默展示张冠李戴的数据：54 是 PCE 不是就业报告，
    21 是货币供应量不是零售销售。"""
    from ta.data.econ import FRED_RELEASES
    assert FRED_RELEASES[10][0] == "CPI"
    assert FRED_RELEASES[50][0] == "非农就业"
    assert FRED_RELEASES[54][0] == "PCE 物价"
    assert FRED_RELEASES[9][0] == "零售销售"
    assert 21 not in FRED_RELEASES


def test_cache_roundtrip(tmp_path, monkeypatch):
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "CACHE_PATH", tmp_path / "econ.json")
    events = [EconEvent(date(2026, 9, 11), "CPI", "通胀", time(8, 30), "fred")]
    econ._cache_put("k", events)
    restored = econ._cache_get("k")
    assert len(restored) == 1
    assert restored[0].day == date(2026, 9, 11)
    assert restored[0].at == time(8, 30)
    assert restored[0].source == "fred"


def test_cache_expires(tmp_path, monkeypatch):
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "CACHE_PATH", tmp_path / "econ.json")
    monkeypatch.setattr(econ, "CACHE_TTL_SECONDS", -1)
    econ._cache_put("k", [EconEvent(date(2026, 9, 11), "CPI", "通胀", None)])
    assert econ._cache_get("k") is None


def test_cache_miss_returns_none(tmp_path, monkeypatch):
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "CACHE_PATH", tmp_path / "econ.json")
    assert econ._cache_get("没有这个键") is None


def test_cache_is_bounded(tmp_path, monkeypatch):
    """缓存文件不能无限增长。"""
    import ta.data.econ as econ
    monkeypatch.setattr(econ, "CACHE_PATH", tmp_path / "econ.json")
    for i in range(60):
        econ._cache_put(f"k{i}", [EconEvent(date(2026, 9, 11), f"E{i}", "x", None)])
    assert len(econ._cache_load()) <= 40


def test_corrupt_cache_is_ignored(tmp_path, monkeypatch):
    import ta.data.econ as econ
    path = tmp_path / "econ.json"
    path.write_text("{ 坏掉的")
    monkeypatch.setattr(econ, "CACHE_PATH", path)
    assert econ._cache_get("k") is None
