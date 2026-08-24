"""报告生成测试：分组、排序、HTML 合法性。"""
from datetime import datetime, timezone

import pytest

from ta.data.base import Quote
from ta.indicators import Snapshot
from ta.reports import Digest, Row, postclose_report, premarket_report


def row(symbol, change, rsi=50.0, vr=1.0):
    price = 100.0
    prev = price / (1 + change / 100)
    q = Quote(symbol=symbol, price=price, prev_close=prev, day_open=prev,
              day_high=price, day_low=price, day_volume=1e6,
              ts=datetime.now(timezone.utc), source="test")
    s = Snapshot(symbol=symbol, close=price, sma={20: 90.0, 50: 85.0, 200: 80.0},
                 ema={}, rsi=rsi, volume_ratio=vr, volume_ratio_projected=False,
                 sma_gap_pct={20: 11.0, 50: 17.0, 200: 25.0})
    return Row(symbol=symbol, quote=q, snap=s)


@pytest.fixture
def digest():
    return Digest(
        rows=[row("NVDA", 3.0), row("KO", -1.0), row("IONQ", -12.0, rsi=15.0),
              row("PLTR", 8.0, rsi=85.0), row("MSFT", 0.2)],
        benchmarks=[row("SPY", 0.5), row("GLD", -0.3)],
    )


def test_movers_sorted_correctly(digest):
    gainers, losers = digest.movers()
    assert [r.symbol for r in gainers] == ["PLTR", "NVDA", "MSFT"]
    assert [r.symbol for r in losers] == ["IONQ", "KO"]


def test_rsi_extremes_split(digest):
    over, under = digest.rsi_extremes()
    assert [r.symbol for r in over] == ["PLTR"]
    assert [r.symbol for r in under] == ["IONQ"]


def test_by_group_uses_config(digest):
    groups = digest.by_group()
    assert "core_mega" in groups
    assert {r.symbol for r in groups["core_mega"]} == {"NVDA", "MSFT"}
    assert {r.symbol for r in groups["high_vol"]} == {"IONQ"}


def test_premarket_mentions_extremes(digest):
    text = premarket_report(digest)
    assert "IONQ" in text and "超卖" in text
    assert "PLTR" in text and "超买" in text


def test_postclose_has_both_sections(digest):
    text = postclose_report(digest)
    assert "领涨" in text and "领跌" in text


def test_html_tags_are_balanced(digest):
    """未闭合的标签会让 Telegram 直接拒收整条消息。"""
    for text in (premarket_report(digest), postclose_report(digest)):
        for tag in ("b", "i", "u", "code"):
            assert text.count(f"<{tag}>") == text.count(f"</{tag}>"), tag


def test_empty_digest_does_not_crash():
    empty = Digest(rows=[], benchmarks=[])
    assert premarket_report(empty)
    assert postclose_report(empty)
