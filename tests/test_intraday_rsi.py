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


def test_summary_mode_reports_when_nothing_triggers(monkeypatch):
    """只说"没有信号"没法让人确信程序在正常工作，
    回报里要带扫描范围和 RSI 区间。"""
    from ta.jobs import _no_signal_report
    text = _no_signal_report({"NVDA": 58.5, "AVGO": 35.9},
                             {"AAPL": 34.2, "TSM": 71.9})
    assert "无标的触及" in text
    assert "AVGO" in text and "35.9" in text      # 离下沿最近
    assert "TSM" in text and "71.9" in text       # 离上沿最近
    assert "2 只" in text


def test_summary_report_handles_missing_data():
    from ta.jobs import _no_signal_report
    text = _no_signal_report({}, {})
    assert "无数据" in text


def test_summary_mode_does_not_consume_dedupe(db, monkeypatch):
    """手动查看不该占用当日去重额度，否则会把后面真正的自动告警吞掉。"""
    import ta.jobs as J
    from ta.reports import Digest

    monkeypatch.setattr(J, "_is_trading_day", lambda router, day=None: True)
    monkeypatch.setattr(J, "_collect", lambda router, syms: Digest(rows=[], benchmarks=[]))
    monkeypatch.setattr(J, "_intraday_rsi_alerts", lambda syms: ([], {}))
    sent = []
    monkeypatch.setattr(J, "_deliver",
                        lambda text, dry, label: sent.append(label) or 0)

    J.job_intraday(force=True, summary=True)
    assert sent == ["无信号回报"]
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_check_job_runs_outside_market_hours(db, monkeypatch):
    """07:00 的盘前巡检本来就在开盘前，不能被交易时段判断挡住。"""
    import ta.jobs as J
    from ta.reports import Digest

    monkeypatch.setattr(J, "is_market_hours", lambda at=None: False)
    monkeypatch.setattr(J, "_is_trading_day", lambda router, day=None: True)
    monkeypatch.setattr(J, "_collect", lambda router, syms: Digest(rows=[], benchmarks=[]))
    monkeypatch.setattr(J, "_intraday_rsi_alerts", lambda syms: ([], {}))
    sent = []
    monkeypatch.setattr(J, "_deliver", lambda text, dry, label: sent.append(label) or 0)

    assert J.job_check() == 0
    assert sent == ["无信号回报"]


def test_check_job_skips_non_trading_days(db, monkeypatch):
    """周末和假日推一条"一切正常"只是噪音。"""
    import ta.jobs as J

    monkeypatch.setattr(J, "_is_trading_day", lambda router, day=None: False)
    sent = []
    monkeypatch.setattr(J, "_deliver", lambda text, dry, label: sent.append(label) or 0)
    assert J.job_check() == 0
    assert sent == []


def test_check_job_respects_switch(db, monkeypatch, isolated_config):
    import re

    import ta.jobs as J
    from ta import config as C

    text = isolated_config.read_text()
    isolated_config.write_text(
        re.sub(r"^(\s+check:\s*)true\b", r"\1false", text, flags=re.M))
    C.config.cache_clear()

    sent = []
    monkeypatch.setattr(J, "_deliver", lambda text, dry, label: sent.append(label) or 0)
    assert J.job_check() == 0
    assert sent == []


def test_report_flags_stale_intraday_when_closed(monkeypatch):
    """休市时分钟线读数是上一时段收尾，不标出来会被误读成实时。"""
    import ta.jobs as J

    monkeypatch.setattr(J, "is_market_hours", lambda at=None: False)
    text = J._no_signal_report({"NVDA": 58.0}, {"NVDA": 44.0})
    assert "上一交易时段收尾" in text

    monkeypatch.setattr(J, "is_market_hours", lambda at=None: True)
    assert "上一交易时段收尾" not in J._no_signal_report({"NVDA": 58.0}, {"NVDA": 44.0})
