"""服务端生成内联 SVG 图表。

不用前端图表库：本地看板没必要引 CDN，断网也能用，
而且 SVG 直接嵌进 HTML，没有加载闪烁。

配色取自 dataviz 规范并经 validate_palette.js 校验：
浅色 #2a78d6/#eb6834/#1baf7a/#eda100，深色为同色相的深色档。
aqua 与 yellow 在浅色底上对比度低于 3:1，故所有均线在末端
带可见的直接标签（规范要求的补救措施），不靠颜色单独承载识别。
"""
from __future__ import annotations

import math

from dataclasses import dataclass

from ta.data.base import Bar
from ta.indicators import sma

# 颜色作为 CSS 变量输出，浅/深两套在样式表里切换
SERIES = {
    "price": "var(--series-1)",
    20: "var(--series-2)",
    50: "var(--series-3)",
    200: "var(--series-4)",
}


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@dataclass
class Scale:
    lo: float
    hi: float
    size: float
    pad: float = 0.0

    def __call__(self, v: float) -> float:
        span = self.hi - self.lo or 1.0
        return self.pad + (1 - (v - self.lo) / span) * (self.size - 2 * self.pad)


def sparkline(bars: list[Bar], width: int = 110, height: int = 28) -> str:
    """行内迷你走势图。单一系列，无需图例（标题即标识）。"""
    closes = [b.close for b in bars]
    if len(closes) < 2:
        return f'<svg width="{width}" height="{height}" role="presentation"></svg>'
    lo, hi = min(closes), max(closes)
    ys = Scale(lo, hi, height, pad=3)
    step = width / (len(closes) - 1)
    pts = " ".join(f"{i * step:.1f},{ys(c):.1f}" for i, c in enumerate(closes))
    rising = closes[-1] >= closes[0]
    #  刻意不用涨跌红绿：相邻的"涨跌"列已经用红绿表示当日方向，
    #  走势线讲的是 30 日形状，两者时间尺度不同，同色系会互相打架
    #  （曾出现 +0.99% 绿字配红色走势线）。方向信息交给 aria-label。
    stroke = "var(--spark-ink)"
    label = "上行" if rising else "下行"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="近期走势{label}">'
        f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{width:.1f}" cy="{ys(closes[-1]):.1f}" r="2" fill="{stroke}"/>'
        f"</svg>"
    )


def nice_ticks(lo: float, hi: float, count: int = 6) -> list[float]:
    """产出 1/2/5×10^k 的整齐刻度，避免出现 181、161 这种随意数字。"""
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.001:
        ticks.append(round(v, 10))
        v += step
    return ticks


def price_chart(bars: list[Bar], periods=(20, 50, 200), window: int = 180,
                width: int = 900, height: int = 340) -> str:
    """价格 + 均线。四条线，带图例与末端直接标签。

    bars 传完整历史，均线先在完整序列上算完再截取显示窗口 ——
    否则 200 日均线在 180 根的窗口里全是 None（图例有、线却不画），
    50 日均线的左半段也会凭空消失。
    """
    if len(bars) < 2:
        return "<p class='empty'>数据不足</p>"

    all_closes = [b.close for b in bars]
    full = {"price": all_closes}
    for p in periods:
        full[p] = sma(all_closes, p)

    bars = bars[-window:]
    lines = {}
    for key, series in full.items():
        cut = series[-window:]
        if any(v is not None for v in cut):
            lines[key] = cut
    closes = lines["price"]

    left, right, top, bottom = 8, 62, 14, 26
    plot_w = width - left - right
    plot_h = height - top - bottom

    values = [v for series in lines.values() for v in series if v is not None]
    lo, hi = min(values), max(values)
    margin = (hi - lo) * 0.06 or 1.0
    ys = Scale(lo - margin, hi + margin, plot_h)
    step = plot_w / (len(closes) - 1)
    ticks = nice_ticks(lo - margin, hi + margin)

    def path(series) -> str:
        pts = [f"{left + i * step:.1f},{top + ys(v):.1f}"
               for i, v in enumerate(series) if v is not None]
        return " ".join(pts)

    out = [
        f'<svg class="price-chart" viewBox="0 0 {width} {height}" '
        f'width="100%" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="价格与均线走势图">'
    ]

    # 网格与刻度：整齐档位的水平参考线，recessive
    for v in ticks:
        y = top + ys(v)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                   f'class="grid"/>')
        out.append(f'<text x="{left + plot_w + 6}" y="{y + 3.5:.1f}" class="tick">'
                   f'{v:,.0f}</text>')

    # 日期刻度：首尾改用 start/end 锚点，居中会溢出画布被截断
    n_ticks = 5
    for i in range(n_ticks):
        idx = round(i * (len(bars) - 1) / (n_ticks - 1))
        x = left + idx * step
        anchor = "start" if i == 0 else ("end" if i == n_ticks - 1 else "middle")
        out.append(f'<text x="{x:.1f}" y="{height - 8}" class="tick" '
                   f'text-anchor="{anchor}">{bars[idx].day.strftime("%m/%d")}</text>')

    # 均线在下、价格在上，保证价格不被遮挡
    for key in list(lines)[::-1]:
        series = lines[key]
        cls = "line-price" if key == "price" else f"line-sma{key}"
        out.append(f'<polyline points="{path(series)}" fill="none" '
                   f'class="{cls}" stroke-linejoin="round"/>')

    # 末端直接标签 —— 规范对低对比度色要求的补救措施
    label_slots = []
    for key, series in lines.items():
        last = next((v for v in reversed(series) if v is not None), None)
        if last is None:
            continue
        label_slots.append((top + ys(last), key, last))
    label_slots.sort()
    prev_y = -99.0
    for y, key, value in label_slots:
        y = max(y, prev_y + 11)          # 防止标签重叠
        prev_y = y
        text = "价格" if key == "price" else f"MA{key}"
        cls = "lbl-price" if key == "price" else f"lbl-sma{key}"
        out.append(f'<text x="{left + plot_w + 6}" y="{y + 3.5:.1f}" '
                   f'class="end-label {cls}">{text}</text>')

    out.append("</svg>")
    return "".join(out)


def rsi_chart(bars: list[Bar], values: list[float | None],
              low: float = 20, high: float = 80,
              width: int = 900, height: int = 120,
              tiers: tuple[list[float], list[float]] | None = None) -> str:
    """RSI 独立面板。绝不与价格共用一个 y 轴 —— 量纲不同，双轴是错的。"""
    pairs = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return "<p class='empty'>RSI 数据不足</p>"

    left, right, top, bottom = 8, 62, 10, 18
    plot_w = width - left - right
    plot_h = height - top - bottom
    ys = Scale(0, 100, plot_h)
    step = plot_w / (len(values) - 1)

    out = [f'<svg class="rsi-chart" viewBox="0 0 {width} {height}" width="100%" '
           f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="RSI 走势图">']

    # 超买/超卖区带。有两档时画两层，越深的一档颜色越重，
    # 一眼能看出当前在浅档还是深档。
    lows = (tiers[0] if tiers else [low])
    highs = (tiers[1] if tiers else [high])
    for lv in highs:
        out.append(f'<rect x="{left}" y="{top + ys(100):.1f}" width="{plot_w}" '
                   f'height="{ys(lv) - ys(100):.1f}" class="band-over"/>')
    for lv in lows:
        out.append(f'<rect x="{left}" y="{top + ys(lv):.1f}" width="{plot_w}" '
                   f'height="{ys(0) - ys(lv):.1f}" class="band-under"/>')

    for level in sorted({*lows, 50, *highs}):
        y = top + ys(level)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                   f'class="grid"/>')
        out.append(f'<text x="{left + plot_w + 6}" y="{y + 3.5:.1f}" class="tick">'
                   f'{level:g}</text>')

    pts = " ".join(f"{left + i * step:.1f},{top + ys(v):.1f}" for i, v in pairs)
    out.append(f'<polyline points="{pts}" fill="none" class="line-rsi" '
               f'stroke-linejoin="round"/>')
    out.append("</svg>")
    return "".join(out)


def rsi_meter(value: float | None, low: float = 20, high: float = 80) -> str:
    """表格里的 RSI 微型量表：位置 + 数值，颜色不单独承载信息。"""
    if value is None:
        return '<span class="muted">-</span>'
    width, height = 56, 10
    x = value / 100 * width
    state = "over" if value >= high else ("under" if value <= low else "mid")
    return (
        f'<span class="meter-wrap"><svg width="{width}" height="{height}" '
        f'role="img" aria-label="RSI {value:.0f}">'
        f'<rect x="0" y="{height / 2 - 1.5}" width="{width}" height="3" class="meter-track"/>'
        f'<rect x="0" y="{height / 2 - 1.5}" width="{low / 100 * width:.1f}" height="3" '
        f'class="meter-under"/>'
        f'<rect x="{high / 100 * width:.1f}" y="{height / 2 - 1.5}" '
        f'width="{width - high / 100 * width:.1f}" height="3" class="meter-over"/>'
        f'<circle cx="{x:.1f}" cy="{height / 2}" r="3.5" class="meter-dot m-{state}"/>'
        f'</svg><b class="meter-val m-{state}">{value:.0f}</b></span>'
    )
