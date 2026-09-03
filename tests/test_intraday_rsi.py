"""分钟线 RSI 信号测试：与日线独立、独立去重、独立成条。"""
from datetime import date, datetime, timezone

import pytest

from ta import store
from ta.alerts import evaluate, evaluate_intraday_rsi, filter_new
from ta.data.base import Quote
from ta.indicators import Snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr("ta.config.DB_PATH", path)
    monkeypatch.setattr("ta.store.DB_PATH", path)
    store.init_db(path)
    return path


def test_oversold_triggers():
    alerts = evaluate_intraday_rsi("NVDA", 15.0, 200.0)
    assert len(alerts) == 1
    assert alerts[0].kind == "rsi_intraday"
    assert alerts[0].tier == "oversold"
    assert "超卖" in alerts[0].headline


def test_overbought_triggers():
    assert evaluate_intraday_rsi("NVDA", 88.0, 200.0)[0].tier == "overbought"


def test_normal_range_silent():
    assert evaluate_intraday_rsi("NVDA", 55.0, 200.0) == []


def test_missing_rsi_silent():
    assert evaluate_intraday_rsi("NVDA", None, 200.0) == []


def test_timeframe_label_in_headline():
    alerts = evaluate_intraday_rsi("NVDA", 15.0, 200.0, timeframe="15 分钟")
    assert "15 分钟 RSI" in alerts[0].headline


def test_deep_extreme_is_severe():
    assert evaluate_intraday_rsi("NVDA", 12.0, 200.0)[0].severity == 2
    assert evaluate_intraday_rsi("NVDA", 19.0, 200.0)[0].severity == 1


def test_daily_and_intraday_dedupe_independently(db):
    """同一只票的日线超卖与 5 分钟超卖是两件事，都该推。"""
    quote = Quote(symbol="KO", price=100.0, prev_close=100.0, day_open=100.0,
                  day_high=100.0, day_low=100.0, day_volume=1e6,
                  ts=datetime.now(timezone.utc), source="test")
    snap = Snapshot(symbol="KO", close=100.0, sma={20: 100.0, 50: 100.0, 200: 100.0},
                    ema={}, rsi=15.0, volume_ratio=1.0,
                    volume_ratio_projected=False, sma_gap_pct={})
    day = date(2026, 9, 3)

    daily = filter_new(evaluate(quote, snap), day=day)
    intraday = filter_new(evaluate_intraday_rsi("KO", 15.0, 100.0), day=day)
    assert len(daily) == 1 and daily[0].kind == "rsi_extreme"
    assert len(intraday) == 1 and intraday[0].kind == "rsi_intraday"


def test_intraday_dedupes_within_day(db):
    day = date(2026, 9, 3)
    assert len(filter_new(evaluate_intraday_rsi("KO", 15.0, 100.0), day=day)) == 1
    assert filter_new(evaluate_intraday_rsi("KO", 14.0, 100.0), day=day) == []


def test_intraday_both_directions_separate(db):
    """同一天里先超卖后超买，两条都该推。"""
    day = date(2026, 9, 3)
    assert len(filter_new(evaluate_intraday_rsi("KO", 15.0, 100.0), day=day)) == 1
    assert len(filter_new(evaluate_intraday_rsi("KO", 85.0, 110.0), day=day)) == 1


def test_new_day_resets(db):
    assert len(filter_new(evaluate_intraday_rsi("KO", 15.0, 100.0),
                          day=date(2026, 9, 3))) == 1
    assert len(filter_new(evaluate_intraday_rsi("KO", 15.0, 100.0),
                          day=date(2026, 9, 4))) == 1


def test_pct_move_respects_switch(isolated_config):
    """涨跌幅告警关掉后，即便跌破阈值也不该产出信号。

    isolated_config 是测试用配置的路径；就地改开关而不重写整份文件，
    以免破坏 symbols 的方括号写法。
    """
    import re

    from ta import config as C

    quote = Quote(symbol="KO", price=80.0, prev_close=100.0, day_open=100.0,
                  day_high=100.0, day_low=80.0, day_volume=1e6,
                  ts=datetime.now(timezone.utc), source="test")
    assert any(a.kind == "pct_move" for a in evaluate(quote, None))

    text = isolated_config.read_text()
    isolated_config.write_text(
        re.sub(r"^(\s+pct_move_alert:\s*)true\b", r"\1false", text, flags=re.M))
    C.config.cache_clear()
    assert not any(a.kind == "pct_move" for a in evaluate(quote, None))
