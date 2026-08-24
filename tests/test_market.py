"""交易时段计算测试。"""
from datetime import datetime

import pytest

from ta.market import ET, is_market_hours, session_fraction


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
