"""图表生成测试：均线覆盖范围、刻度整齐度、SVG 合法性。"""
from datetime import date, timedelta

import pytest

from ta.data.base import Bar
from ta.web.charts import nice_ticks, price_chart, rsi_chart, rsi_meter, sparkline


def bars(n: int) -> list[Bar]:
    start = date(2025, 1, 1)
    return [Bar(day=start + timedelta(days=i), open=100 + i * 0.1, high=101 + i * 0.1,
                low=99 + i * 0.1, close=100 + i * 0.1, volume=1e6) for i in range(n)]


def test_nice_ticks_are_round():
    """刻度必须落在 1/2/2.5/5 ×10^k 的档位上，不能是 181、161 这种。"""
    ticks = nice_ticks(161.3, 243.7)
    step = ticks[1] - ticks[0]
    assert step in (10, 20, 25, 50), step
    assert all(abs(t / step - round(t / step)) < 1e-9 for t in ticks), ticks


def test_nice_ticks_cover_range():
    lo, hi = 12.4, 88.9
    ticks = nice_ticks(lo, hi)
    assert ticks[0] >= lo and ticks[-1] <= hi
    assert len(ticks) >= 3


def test_nice_ticks_degenerate_range():
    assert nice_ticks(50.0, 50.0) == [50.0]


def test_ma200_drawn_when_history_exceeds_window():
    """曾经的 bug：均线在 180 根的显示窗口上计算，
    MA200 全是 None —— 图例有、线不画。"""
    svg = price_chart(bars(400), window=180)
    assert 'class="line-sma200"' in svg
    assert "lbl-sma200" in svg


def test_ma50_spans_full_window():
    """MA50 应当从窗口最左端就有值，而不是过了 50 根才开始。"""
    svg = price_chart(bars(400), window=180)
    seg = svg.split('class="line-sma50"')[0].rsplit("<polyline", 1)[1]
    first_x = float(seg.split('points="')[1].split(",")[0])
    assert first_x < 12, f"MA50 起点 x={first_x}，应贴近左边界"


def test_ma200_absent_when_history_too_short():
    svg = price_chart(bars(120), window=180)
    assert 'class="line-sma200"' not in svg


def test_edge_date_labels_do_not_overflow():
    """首尾日期标签用 start/end 锚点，居中会被画布边缘截断。"""
    svg = price_chart(bars(300), window=180)
    assert 'text-anchor="start"' in svg
    assert 'text-anchor="end"' in svg


def test_svg_tags_balanced():
    for svg in (sparkline(bars(30)), price_chart(bars(300)),
                rsi_chart(bars(60), [50.0] * 60)):
        assert svg.count("<svg") == svg.count("</svg>")
        assert svg.count("<text") == svg.count("</text>")


def test_sparkline_is_neutral_not_red_green():
    """走势线不用涨跌红绿：同一行的"涨跌"列已经占用了这套语义。"""
    up, down = sparkline(bars(30)), sparkline(bars(30)[::-1])
    assert "--delta-up" not in up and "--delta-down" not in down
    assert "--spark-ink" in up


def test_sparkline_direction_in_aria_label():
    assert "上行" in sparkline(bars(30))
    assert "下行" in sparkline(bars(30)[::-1])


def test_charts_handle_insufficient_data():
    assert "数据不足" in price_chart(bars(1))
    assert "RSI 数据不足" in rsi_chart(bars(5), [None] * 5)
    assert "<svg" in sparkline(bars(1))


def test_rsi_meter_states():
    assert "m-under" in rsi_meter(15.0)
    assert "m-over" in rsi_meter(85.0)
    assert "m-mid" in rsi_meter(50.0)
    assert "-" in rsi_meter(None)
