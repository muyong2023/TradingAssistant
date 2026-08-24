"""晨报与盘后报告的正文生成。

现在是纯模板；第 3 步接入 Claude 后，这里产出的结构化数据会作为
事实依据交给模型润色成叙述，模板版保留为 LLM 不可用时的降级路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ta.config import config, watchlists
from ta.data.base import Quote
from ta.indicators import Snapshot
from ta.market import now_et
from ta.notify.telegram import escape


@dataclass
class Row:
    symbol: str
    quote: Quote | None
    snap: Snapshot | None

    @property
    def change(self) -> float:
        return self.quote.change_pct if self.quote else 0.0

    @property
    def price(self) -> float:
        if self.quote:
            return self.quote.price
        return self.snap.close if self.snap else 0.0


@dataclass
class Digest:
    """一次扫描的结构化结果，报告和看板共用。"""
    rows: list[Row] = field(default_factory=list)
    benchmarks: list[Row] = field(default_factory=list)

    def by_group(self) -> dict[str, list[Row]]:
        groups = watchlists()
        out: dict[str, list[Row]] = {}
        for name, group in groups.items():
            members = [r for r in self.rows if r.symbol in group["symbols"]]
            if members:
                out[name] = members
        return out

    def movers(self, n: int = 5) -> tuple[list[Row], list[Row]]:
        ranked = sorted(self.rows, key=lambda r: r.change, reverse=True)
        gainers = [r for r in ranked if r.change > 0][:n]
        losers = [r for r in reversed(ranked) if r.change < 0][:n]
        return gainers, losers

    def rsi_extremes(self) -> tuple[list[Row], list[Row]]:
        cfg = config()["indicators"]["rsi"]
        over, under = [], []
        for r in self.rows:
            if not r.snap or r.snap.rsi is None:
                continue
            if r.snap.rsi >= cfg["overbought"]:
                over.append(r)
            elif r.snap.rsi <= cfg["oversold"]:
                under.append(r)
        return over, under


def _fmt_row(r: Row) -> str:
    rsi = f"{r.snap.rsi:.0f}" if r.snap and r.snap.rsi is not None else "-"
    return (f"<code>{r.symbol:<5}</code> ${r.price:>9,.2f}  "
            f"{r.change:+6.2f}%  RSI {rsi:>3}  {r.snap.trend() if r.snap else ''}")


def _benchmark_line(digest: Digest) -> str:
    if not digest.benchmarks:
        return ""
    parts = [f"{r.symbol} {r.change:+.2f}%" for r in digest.benchmarks]
    return "📊 " + "  ·  ".join(parts)


def premarket_report(digest: Digest) -> str:
    """开盘前 30 分钟推送：昨日收盘状态 + 今日关注点。"""
    ts = now_et().strftime("%Y-%m-%d %a")
    lines = [f"<b>☀️ 盘前简报</b>  {ts}", ""]

    bench = _benchmark_line(digest)
    if bench:
        lines += [f"<i>基准（昨收）</i>", bench, ""]

    over, under = digest.rsi_extremes()
    watch: list[str] = []
    for r in under:
        watch.append(f"• {r.symbol} RSI {r.snap.rsi:.0f} 超卖，{r.snap.trend()}")
    for r in over:
        watch.append(f"• {r.symbol} RSI {r.snap.rsi:.0f} 超买，{r.snap.trend()}")
    for r in digest.rows:
        if r.snap and r.snap.volume_ratio and r.snap.volume_ratio >= 2.0:
            watch.append(f"• {r.symbol} 昨日放量 {r.snap.volume_ratio:.1f}x")
    if watch:
        lines += ["<b>今日关注</b>"] + watch + [""]
    else:
        lines += ["<i>无 RSI 极值或异常放量</i>", ""]

    lines.append("<b>持仓状态</b>")
    for name, members in digest.by_group().items():
        label = escape(watchlists()[name]["label"])
        lines.append(f"\n<u>{label}</u>")
        lines += [_fmt_row(r) for r in members]
    return "\n".join(lines)


def postclose_report(digest: Digest) -> str:
    """收盘后 15 分钟推送：当日复盘。"""
    ts = now_et().strftime("%Y-%m-%d %a")
    lines = [f"<b>🌙 盘后复盘</b>  {ts}", ""]

    bench = _benchmark_line(digest)
    if bench:
        lines += [bench, ""]

    gainers, losers = digest.movers()
    if gainers:
        lines.append("<b>📈 领涨</b>")
        lines += [_fmt_row(r) for r in gainers]
        lines.append("")
    if losers:
        lines.append("<b>📉 领跌</b>")
        lines += [_fmt_row(r) for r in losers]
        lines.append("")

    over, under = digest.rsi_extremes()
    if over or under:
        lines.append("<b>RSI 极值</b>")
        lines += [f"• {r.symbol} 超卖 {r.snap.rsi:.0f}" for r in under]
        lines += [f"• {r.symbol} 超买 {r.snap.rsi:.0f}" for r in over]
        lines.append("")

    flat = [r for r in digest.rows if abs(r.change) < 0.5]
    lines.append(f"<i>共 {len(digest.rows)} 只，其中 {len(flat)} 只波动小于 0.5%</i>")
    return "\n".join(lines)
