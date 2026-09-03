"""告警规则测试：分档、方向、去重。"""
from datetime import date, datetime, timezone

import pytest

from ta import store
from ta.alerts import evaluate, filter_new
from ta.data.base import Quote
from ta.indicators import Snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr("ta.config.DB_PATH", path)
    monkeypatch.setattr("ta.store.DB_PATH", path)
    store.init_db(path)
    return path


def quote(symbol, price, prev):
    return Quote(symbol=symbol, price=price, prev_close=prev, day_open=prev,
                 day_high=price, day_low=price, day_volume=1e6,
                 ts=datetime.now(timezone.utc), source="test")


def snap(symbol, rsi=50.0):
    return Snapshot(symbol=symbol, close=100.0, sma={20: 100.0, 50: 100.0, 200: 100.0},
                    ema={}, rsi=rsi, volume_ratio=1.0, volume_ratio_projected=False,
                    sma_gap_pct={})


def test_no_alert_within_threshold():
    # KO 属 defensive，阈值 ±5%
    assert evaluate(quote("KO", 97.0, 100.0), snap("KO")) == []


def test_pct_alert_fires_at_group_threshold():
    alerts = evaluate(quote("KO", 94.0, 100.0), snap("KO"))
    assert [a.kind for a in alerts] == ["pct_move"]
    assert "跌 6.00%" in alerts[0].headline


def test_high_vol_group_needs_bigger_move():
    """IONQ 属 high_vol（±15%），跌 6% 不该报 —— 这是分组阈值的意义。"""
    assert evaluate(quote("IONQ", 94.0, 100.0), snap("IONQ")) == []
    assert evaluate(quote("IONQ", 84.0, 100.0), snap("IONQ"))[0].kind == "pct_move"


def test_reports_highest_tier_only():
    alerts = evaluate(quote("KO", 89.0, 100.0), snap("KO"))   # -11%，跨过 5 和 9 两档
    assert len(alerts) == 1
    assert alerts[0].tier == "9down"
    assert alerts[0].severity == 2


def test_up_and_down_are_separate_tiers():
    up = evaluate(quote("KO", 106.0, 100.0), snap("KO"))[0]
    down = evaluate(quote("KO", 94.0, 100.0), snap("KO"))[0]
    assert up.tier != down.tier
    assert "涨" in up.headline and "跌" in down.headline


def test_rsi_oversold_alert():
    alerts = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=18.0))
    assert [a.kind for a in alerts] == ["rsi_extreme"]
    assert alerts[0].tier == "oversold20"


def test_rsi_overbought_alert():
    alerts = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=85.0))
    assert alerts[0].tier == "overbought80"


def test_rsi_normal_no_alert():
    assert evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=55.0)) == []


def test_rsi_outer_tier_alert():
    """两档：先到 30 就该报一次。"""
    a = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=28.0))[0]
    assert a.tier == "oversold30"
    assert a.severity == 1          # 外档，一般


def test_rsi_inner_tier_is_severe():
    a = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=18.0))[0]
    assert a.severity == 2          # 内档，重要


def test_rsi_reports_deepest_tier_only():
    """一次从 35 跌到 18 只报最深那档，不必连报两条。"""
    alerts = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=18.0))
    assert len(alerts) == 1
    assert alerts[0].tier == "oversold20"


def test_rsi_tiers_dedupe_independently(db):
    """先到 28 报过 30 档，继续跌到 18 仍应再报 20 档。"""
    from datetime import date as _date
    day = _date(2026, 9, 4)
    first = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=28.0))
    assert len(filter_new(first, day=day)) == 1
    again = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=28.5))
    assert filter_new(again, day=day) == []          # 同档不重复
    deeper = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=18.0))
    assert len(filter_new(deeper, day=day)) == 1     # 深一档再报


def test_pct_and_rsi_can_both_fire():
    alerts = evaluate(quote("KO", 90.0, 100.0), snap("KO", rsi=15.0))
    assert {a.kind for a in alerts} == {"pct_move", "rsi_extreme"}


def test_filter_new_dedupes(db):
    alerts = evaluate(quote("KO", 94.0, 100.0), snap("KO"))
    assert len(filter_new(alerts, day=date(2026, 8, 24))) == 1
    assert filter_new(alerts, day=date(2026, 8, 24)) == []


def test_escalation_still_pushes(db):
    """先跌 6% 推过了，继续跌到 11% 应当再推一次（不同档位）。"""
    first = evaluate(quote("KO", 94.0, 100.0), snap("KO"))
    assert len(filter_new(first, day=date(2026, 8, 24))) == 1
    second = evaluate(quote("KO", 89.0, 100.0), snap("KO"))
    assert len(filter_new(second, day=date(2026, 8, 24))) == 1


def test_new_day_resets_dedupe(db):
    alerts = evaluate(quote("KO", 94.0, 100.0), snap("KO"))
    assert len(filter_new(alerts, day=date(2026, 8, 24))) == 1
    assert len(filter_new(alerts, day=date(2026, 8, 25))) == 1


def test_missing_snapshot_still_reports_pct(db):
    alerts = evaluate(quote("KO", 94.0, 100.0), None)
    assert [a.kind for a in alerts] == ["pct_move"]


def test_big_move_outranks_marginal_rsi():
    """曾经的 bug：跌 19.5% 被标成一般，刚压线的 RSI 却标成重要。"""
    big = evaluate(quote("IONQ", 33.0, 41.0), snap("IONQ", rsi=48.0))[0]
    marginal = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=29.5))[0]
    assert big.severity == 2
    assert marginal.severity == 1


def test_deep_rsi_is_severe():
    deep = evaluate(quote("KO", 100.0, 100.0), snap("KO", rsi=12.0))[0]
    assert deep.severity == 2


def test_first_tier_move_is_not_severe():
    mild = evaluate(quote("KO", 94.5, 100.0), snap("KO"))[0]   # -5.5%，刚过 ±5% 首档
    assert mild.severity == 1


def test_magnitude_orders_by_relative_extremity():
    """不同分组的票要能公平比较：按超出各自阈值的倍数排。"""
    ko = evaluate(quote("KO", 90.0, 100.0), snap("KO"))[0]        # -10%, 阈值 5 -> 2.0x
    ionq = evaluate(quote("IONQ", 82.0, 100.0), snap("IONQ"))[0]  # -18%, 阈值 15 -> 1.2x
    assert ko.magnitude > ionq.magnitude


def test_render_groups_by_symbol():
    from ta.alerts import render
    alerts = (evaluate(quote("IONQ", 33.0, 41.0), snap("IONQ", rsi=15.0)))
    assert len(alerts) == 2
    out = render(alerts)
    assert out.count("<b>IONQ</b>") == 1     # 合并成一个块，不重复出现


def test_render_orders_strongest_first():
    from ta.alerts import render
    alerts = (evaluate(quote("KO", 90.0, 100.0), snap("KO"))
              + evaluate(quote("IONQ", 84.0, 100.0), snap("IONQ")))
    out = render(alerts)
    assert out.index("KO") < out.index("IONQ")


def test_absolute_floor_marks_double_digit_moves_severe():
    """任何分组里的双位数单日跌幅都算重要，即使只是该组首档的 1.3 倍。"""
    a = evaluate(quote("IONQ", 33.0, 41.0), snap("IONQ"))[0]   # -19.5%, 首档 15%
    assert a.severity == 2


def test_single_digit_move_in_high_vol_group_stays_normal():
    a = evaluate(quote("IONQ", 84.5, 100.0), snap("IONQ"))[0]  # -15.5%
    assert a.severity == 2      # 超 10% 绝对线
    b = evaluate(quote("KO", 94.5, 100.0), snap("KO"))[0]      # -5.5%
    assert b.severity == 1
