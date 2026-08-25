"""晨报与盘后报告的正文生成。

现在是纯模板；第 3 步接入 Claude 后，这里产出的结构化数据会作为
事实依据交给模型润色成叙述，模板版保留为 LLM 不可用时的降级路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ta.config import alert_tiers, config, watchlists
from ta.data.base import Quote
from ta.indicators import Snapshot
from ta.market import now_et
from ta.data.news import NewsItem
from ta.earnings import EarningsEvent
from ta.macro import MacroEvent, classify
from ta.market import ET, now_et
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


def notable_symbols(digest: Digest) -> set[str]:
    """今天本来就该多看一眼的标的：RSI 极值，或波动已达本组首档阈值。

    新闻排序拿它做加权 —— 有异动的票，它的新闻更值得先读。
    """
    out: set[str] = set()
    over, under = digest.rsi_extremes()
    out.update(r.symbol for r in over + under)
    for r in digest.rows:
        tiers = alert_tiers(r.symbol)
        if tiers and abs(r.change) >= min(tiers):
            out.add(r.symbol)
    return out


def format_news(items: list[NewsItem], releases: list[NewsItem] | None = None) -> list[str]:
    """渲染新闻区块。标题带链接，Telegram 侧已关闭链接预览。"""
    lines = ["<b>📰 隔夜要闻</b>"]
    for item in items:
        syms = "/".join(item.symbols[:3])
        when = item.created_at.astimezone(ET).strftime("%m-%d %H:%M")
        headline = escape(item.headline)
        title = f'<a href="{escape(item.url)}">{headline}</a>' if item.url else headline
        lines.append(f"• <b>{syms}</b> {title}")
        lines.append(f"  <i>{escape(item.source)} · {when} ET</i>")

    if releases:
        core = [(classify(r.headline), r) for r in releases]
        major = [(tag, r) for tag, r in core if tag]
        routine = [r for tag, r in core if not tag]
        if major:
            #  CPI、非农、利率决议这类会推动全市场，必须单独列出，
            #  不能和房价指数、零售连锁销售挤在同一行里
            lines += ["", "<b>🔔 核心数据</b>"]
            lines += [f"• <b>[{tag}]</b> {escape(r.headline)}" for tag, r in major]
        if routine:
            joined = "　".join(escape(r.headline) for r in routine)
            lines += ["", "<b>📈 其他数据</b>", f"<i>{joined}</i>"]
    return lines


def format_calendar(events: list[MacroEvent], today,
                    fomc: MacroEvent | None = None) -> list[str]:
    """未来几天的宏观日程。今天的事件单独标出来。"""
    if not events and not fomc:
        return []
    lines = ["<b>📅 宏观日程</b>"]
    for e in events:
        when = e.at.strftime("%H:%M") if e.at else "全天"
        if e.day == today:
            lines.append(f"• <b>今天 {when} ET　{escape(e.label())}</b>")
        else:
            delta = (e.day - today).days
            prefix = "明天" if delta == 1 else e.day.strftime("%m-%d")
            lines.append(f"• {prefix} {when} ET　{escape(e.label())}")

    #  FOMC 常在窗口之外，但它是日程里最重要的一项，单独带倒计时展示
    if fomc and not any(e.day == fomc.day and "FOMC" in e.name for e in events):
        days = (fomc.day - today).days
        lines.append(f"• <b>下次 FOMC</b>　{fomc.day.strftime('%m-%d')}"
                     f"（{days} 天后）　{escape(fomc.detail)}")
    return lines


def format_earnings(events: list[EarningsEvent], today) -> list[str]:
    """财报日程。今明两天的加粗 —— 财报前后波动最大，需要提前知道。"""
    if not events:
        return []
    lines = ["<b>📊 财报日程</b>"]
    for e in events:
        delta = (e.day - today).days
        if delta <= 0:
            prefix, bold = "今天", True
        elif delta == 1:
            prefix, bold = "明天", True
        else:
            prefix, bold = e.day.strftime("%m-%d"), False
        text = f"{prefix}　{escape(e.label())}"
        lines.append(f"• <b>{text}</b>" if bold else f"• {text}")
    return lines


def premarket_report(digest: Digest, news: list[NewsItem] | None = None,
                     releases: list[NewsItem] | None = None,
                     calendar: list[MacroEvent] | None = None,
                     fomc: MacroEvent | None = None,
                     earnings: list[EarningsEvent] | None = None) -> str:
    """开盘前 30 分钟推送：宏观日程 + 隔夜消息 + 收盘状态 + 今日关注点。"""
    ts = now_et().strftime("%Y-%m-%d %a")
    lines = [f"<b>☀️ 盘前简报</b>  {ts}", ""]

    bench = _benchmark_line(digest)
    if bench:
        lines += [f"<i>基准（昨收）</i>", bench, ""]

    today = now_et().date()
    if calendar or fomc:
        #  放在最前：今天有没有 CPI 或利率决议，决定了整天怎么看盘
        lines += format_calendar(calendar or [], today, fomc) + [""]

    if earnings:
        lines += format_earnings(earnings, today) + [""]

    if news or releases:
        lines += format_news(news or [], releases) + [""]

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
