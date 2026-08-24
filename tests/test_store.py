"""存储层测试，重点验证告警去重不会漏推也不会重复推。"""
from datetime import date

import pytest

from ta import store
from ta.data.base import Bar


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


def test_save_bars_is_idempotent(db):
    bars = [Bar(date(2026, 8, 24), 1, 2, 0.5, 1.5, 1000)]
    store.save_bars("NVDA", bars, db)
    store.save_bars("NVDA", bars, db)
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1


def test_record_alert_dedupes_same_day(db):
    assert store.record_alert("NVDA", "pct_move", "7", path=db) is True
    assert store.record_alert("NVDA", "pct_move", "7", path=db) is False


def test_different_tier_alerts_separately(db):
    # 先跌 7% 推一次，继续跌到 12% 应当再推一次
    assert store.record_alert("NVDA", "pct_move", "7", path=db) is True
    assert store.record_alert("NVDA", "pct_move", "12", path=db) is True


def test_different_day_alerts_again(db):
    assert store.record_alert("NVDA", "pct_move", "7", day=date(2026, 8, 24), path=db) is True
    assert store.record_alert("NVDA", "pct_move", "7", day=date(2026, 8, 25), path=db) is True


def test_already_alerted_reflects_record(db):
    assert store.already_alerted("KO", "rsi_extreme", "oversold", path=db) is False
    store.record_alert("KO", "rsi_extreme", "oversold", path=db)
    assert store.already_alerted("KO", "rsi_extreme", "oversold", path=db) is True
