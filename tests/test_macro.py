"""宏观日历与数据分级测试。"""
from datetime import date, time

import pytest

from ta.macro import (MacroEvent, classify, parse_extra, parse_fomc,
                      recurring_events)

FOMC_HTML = """
<div>2026 FOMC Meetings</div>
<div class="fomc-meeting__month col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-lg-1">27-28</div>
<div class="fomc-meeting__month col-md-2"><strong>March</strong></div>
<div class="fomc-meeting__date col-lg-1">17-18*</div>
<div class="fomc-meeting__month col-md-2"><strong>April/May</strong></div>
<div class="fomc-meeting__date col-lg-1">28-1</div>
<div>2027 FOMC Meetings</div>
<div class="fomc-meeting__month col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-lg-1">26-27</div>
"""


def test_parse_fomc_extracts_all_years():
    events = parse_fomc(FOMC_HTML)
    assert [e.day for e in events] == [
        date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 1), date(2027, 1, 27)]


def test_parse_fomc_marks_projection_meetings():
    """带星号的会议附带经济预测与记者会，市场关注度显著更高。"""
    events = parse_fomc(FOMC_HTML)
    assert "经济预测" in events[1].detail       # March 17-18*
    assert "经济预测" not in events[0].detail   # January 27-28


def test_parse_fomc_cross_month_uses_end_month():
    """April/May 28-1 的结束日在 5 月 1 日，不是 4 月 1 日。"""
    assert parse_fomc(FOMC_HTML)[2].day == date(2026, 5, 1)


def test_parse_fomc_on_garbage_returns_empty():
    assert parse_fomc("<html>结构变了</html>") == []


def test_recurring_jobless_claims_every_thursday():
    events = recurring_events(date(2026, 8, 24), date(2026, 9, 6))
    claims = [e.day for e in events if e.name == "初请失业金"]
    assert claims == [date(2026, 8, 27), date(2026, 9, 3)]
    assert all(d.weekday() == 3 for d in claims)


def test_recurring_nfp_first_friday_only():
    events = recurring_events(date(2026, 9, 1), date(2026, 9, 30))
    nfp = [e.day for e in events if e.name == "非农就业"]
    assert nfp == [date(2026, 9, 4)]


def test_nfp_marked_unconfirmed():
    """非农偶因假期挪期，规则推导的日期必须标注为预计。"""
    events = recurring_events(date(2026, 9, 1), date(2026, 9, 30))
    nfp = next(e for e in events if e.name == "非农就业")
    assert nfp.confirmed is False
    assert "预计" in nfp.label()


def test_jobless_claims_marked_confirmed():
    events = recurring_events(date(2026, 8, 27), date(2026, 8, 27))
    assert events[0].confirmed is True
    assert "预计" not in events[0].label()


@pytest.mark.parametrize("headline,expected", [
    ("US CPI For August Rises 2.9% YoY", "通胀"),
    ("Core PCE Price Index YoY 2.6%", "通胀"),
    ("Nonfarm Payrolls Up 175K", "就业"),
    ("Initial Jobless Claims 218K", "就业"),
    ("ADP National Employment Report: 54K Jobs Added", "就业"),
    ("Fed Interest Rate Decision Held At 4.25%", "联储"),
    ("US Retail Sales For July Up 0.6%", "消费"),
    ("ISM Manufacturing PMI 48.7", "景气"),
])
def test_classify_core_releases(headline, expected):
    assert classify(headline) == expected


@pytest.mark.parametrize("headline", [
    "Redbook Retail Sales Index Up 9.1% YoY For Week Ended 8/22/26",
    "ADP Employment Change Weekly 11.75K Vs 9.50K Prior",
    "USA House Price Index For June 442.5 Vs 442.4 Prior",
    "USA Building Permits For July Revised To 1.433M",
])
def test_classify_excludes_minor_series(headline):
    """同名的次要系列必须排除。曾用负向前瞻 `Retail Sales(?!.*Redbook)`
    误判 Redbook 为核心消费数据 —— Redbook 出现在词组之前，前瞻无效。"""
    assert classify(headline) is None


def test_parse_extra_accepts_config_entries():
    events = parse_extra([{"date": "2026-09-11", "name": "CPI",
                           "detail": "8 月通胀", "time": "08:30"}])
    assert events[0].day == date(2026, 9, 11)
    assert events[0].at == time(8, 30)
    assert events[0].confirmed is True


def test_parse_extra_skips_bad_entry_without_failing_others():
    """一条写错不该拖垮整个日历。"""
    events = parse_extra([
        {"date": "not-a-date", "name": "坏的"},
        {"date": "2026-09-11", "name": "好的"},
    ])
    assert [e.name for e in events] == ["好的"]


def test_parse_extra_empty():
    assert parse_extra(None) == []


def test_upcoming_prefers_econ_source_over_rules(monkeypatch):
    """econ 源已给出初请失业金时，规则推导的同名事件必须丢弃，
    否则同一件事会出现两次、日期还可能不一致。"""
    from datetime import time as _t

    import ta.macro as m
    from ta.data.econ import EconEvent

    monkeypatch.setattr(m, "fomc_meetings", lambda: [])
    monkeypatch.setattr(
        "ta.data.econ.upcoming",
        lambda start, days: [EconEvent(date(2026, 8, 27), "初请失业金", "就业", _t(8, 30))],
    )
    events = m.upcoming(7, today=date(2026, 8, 24))
    claims = [e for e in events if e.name == "初请失业金"]
    assert len(claims) == 1
    assert claims[0].category == "就业"      # 来自 econ 源而非规则


def test_upcoming_falls_back_to_rules_when_econ_empty(monkeypatch):
    import ta.macro as m
    monkeypatch.setattr(m, "fomc_meetings", lambda: [])
    monkeypatch.setattr("ta.data.econ.upcoming", lambda start, days: [])
    events = m.upcoming(7, today=date(2026, 8, 24))
    assert any(e.name == "初请失业金" for e in events)


def test_upcoming_survives_econ_failure(monkeypatch):
    """经济日历挂掉不能让整份日历失败。"""
    import ta.macro as m

    def boom(start, days):
        raise RuntimeError("网络挂了")

    monkeypatch.setattr(m, "fomc_meetings", lambda: [])
    monkeypatch.setattr("ta.data.econ.upcoming", boom)
    events = m.upcoming(7, today=date(2026, 8, 24))
    assert any(e.name == "初请失业金" for e in events)


def test_label_does_not_repeat_category():
    e = MacroEvent(date(2026, 8, 25), "消费者信心", detail="信心",
                   category="信心", at=None)
    assert e.label() == "[信心] 消费者信心"
