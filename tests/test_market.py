"""交易时段计算测试。"""
from datetime import datetime

import pytest

from ta.market import ET, is_market_hours, last_session_close, session_fraction


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_market_hours_weekday():
    assert is_market_hours(at(2026, 8, 24, 10, 0)) is True
    assert is_market_hours(at(2026, 8, 24, 9, 0)) is False
    assert is_market_hours(at(2026, 8, 24, 16, 0)) is False


def test_market_closed_on_weekend():
    assert is_market_hours(at(2026, 8, 22, 10, 0)) is False


def test_session_fraction_midday():
    # 12:45 ET 恰好是 6.5 小时时段的一半
    assert session_fraction(at(2026, 8, 24, 12, 45)) == pytest.approx(0.5)


def test_session_fraction_before_open_is_full():
    """盘前最新 bar 还是昨天的完整数据，不该被折算放大。"""
    assert session_fraction(at(2026, 8, 24, 8, 0)) == 1.0


def test_session_fraction_after_close_is_full():
    assert session_fraction(at(2026, 8, 24, 16, 30)) == 1.0


def test_session_fraction_at_open_is_floored():
    """开盘瞬间不能除以 0 把量比放大到无穷。"""
    assert session_fraction(at(2026, 8, 24, 9, 30)) == 0.02


def test_last_close_weekday_morning():
    """周二早上 09:00 -> 周一 16:00。"""
    assert last_session_close(at(2026, 8, 25, 9, 0)) == at(2026, 8, 24, 16, 0)


def test_last_close_monday_morning_reaches_back_to_friday():
    """周一 09:00 的隔夜区间必须覆盖上周五收盘后 —— 跨了 65 小时，
    固定回溯 18 小时会漏掉整个周末的消息。"""
    assert last_session_close(at(2026, 8, 24, 9, 0)) == at(2026, 8, 21, 16, 0)


def test_last_close_after_today_close():
    """当天收盘后，起点就是当天 16:00。"""
    assert last_session_close(at(2026, 8, 25, 17, 0)) == at(2026, 8, 25, 16, 0)


def test_last_close_during_session_uses_previous_day():
    assert last_session_close(at(2026, 8, 25, 11, 0)) == at(2026, 8, 24, 16, 0)


def test_last_close_from_weekend():
    assert last_session_close(at(2026, 8, 23, 12, 0)) == at(2026, 8, 21, 16, 0)
