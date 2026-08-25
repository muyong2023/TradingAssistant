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
