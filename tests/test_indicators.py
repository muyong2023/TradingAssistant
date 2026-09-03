"""指标的正确性测试。RSI 用 Wilder 原书的经典数列做基准。"""
import math

import pytest

from ta.indicators import ema, rsi, sma, volume_ratio


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_window_slides_correctly():
    # 长序列上确认没有累加漂移
    values = list(range(1, 101))
    out = sma(values, 10)
    assert out[9] == pytest.approx(5.5)
    assert out[-1] == pytest.approx(95.5)


def test_ema_seeds_with_sma():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)          # 种子 = (1+2+3)/3
    assert out[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5)


def test_rsi_wilder_reference():
    """Wilder《New Concepts》中的标准 14 日样例，期望值 ~70.53。"""
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ]
    out = rsi(closes, 14)
    assert out[13] is None      # 第 14 个收盘价才产出第一个读数
    assert out[14] == pytest.approx(70.53, abs=0.1)


def test_rsi_all_gains_is_100():
    assert rsi(list(range(1, 30)), 14)[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    assert rsi(list(range(30, 1, -1)), 14)[-1] == pytest.approx(0.0)


def test_rsi_insufficient_data_is_all_none():
    assert rsi([1, 2, 3], 14) == [None, None, None]


def test_volume_ratio():
    vols = [100.0] * 20 + [250.0]
    assert volume_ratio(vols, 20) == pytest.approx(2.5)


def test_volume_ratio_insufficient_data():
    assert volume_ratio([100.0] * 5, 20) is None


def test_volume_ratio_zero_baseline():
    assert volume_ratio([0.0] * 20 + [10.0], 20) is None


def test_output_length_matches_input():
    values = [float(i) for i in range(50)]
    for fn, arg in ((sma, 20), (ema, 12), (rsi, 14)):
        assert len(fn(values, arg)) == len(values)


def _snap(close, s20, s50, s200):
    from ta.indicators import Snapshot
    return Snapshot(
        symbol="X", close=close,
        sma={20: s20, 50: s50, 200: s200}, ema={},
        rsi=None, volume_ratio=None, volume_ratio_projected=False, sma_gap_pct={},
    )


def test_trend_bullish_stack():
    assert _snap(110, 105, 100, 90).trend() == "多头排列"


def test_trend_bearish_stack():
    assert _snap(80, 85, 90, 100).trend() == "空头排列"


def test_trend_price_above_all_but_no_golden_cross():
    """GLD 那种情况：价格站上三条均线，但 50 日线还在 200 日线下方。
    旧逻辑会误标成"偏空整理"。"""
    assert _snap(110, 105, 100, 108).trend() == "底部反转中"


def test_trend_price_below_all_but_golden_cross():
    assert _snap(80, 85, 95, 90).trend() == "高位回落"


def test_trend_insufficient_data():
    assert _snap(100, None, None, None).trend() == "数据不足"


def test_volume_ratio_projects_partial_session():
    """盘中只走了一半时段、量已达均量的 60%，全日预估应为 1.2x。"""
    vols = [100.0] * 20 + [60.0]
    assert volume_ratio(vols, 20, session_fraction=0.5) == pytest.approx(1.2)


def test_volume_ratio_no_projection_after_close():
    vols = [100.0] * 20 + [60.0]
    assert volume_ratio(vols, 20, session_fraction=1.0) == pytest.approx(0.6)


def test_rsi_tiers_parses_list():
    from ta.indicators import rsi_tiers
    low, high = rsi_tiers({"oversold": [20, 30], "overbought": [80, 70]})
    assert low == [30.0, 20.0]      # 由浅入深
    assert high == [70.0, 80.0]


def test_rsi_tiers_accepts_scalar():
    """单个数值的旧写法要继续能用。"""
    from ta.indicators import rsi_tiers
    assert rsi_tiers({"oversold": 20, "overbought": 80}) == ([20.0], [80.0])


def test_rsi_zone_returns_outermost():
    from ta.indicators import rsi_zone
    assert rsi_zone({"oversold": [30, 20], "overbought": [70, 80]}) == (30.0, 70.0)


@pytest.mark.parametrize("value,expected", [
    (85, ("overbought", 80.0)),
    (78, ("overbought", 70.0)),
    (70, ("overbought", 70.0)),
    (68, None),
    (50, None),
    (32, None),
    (30, ("oversold", 30.0)),
    (28, ("oversold", 30.0)),
    (19, ("oversold", 20.0)),
    (5, ("oversold", 20.0)),
])
def test_rsi_hit_picks_deepest_tier(value, expected):
    from ta.indicators import rsi_hit
    assert rsi_hit(value, {"oversold": [30, 20], "overbought": [70, 80]}) == expected
