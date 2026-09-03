"""定时任务入口。launchd 调用的就是这里。

    python -m ta.jobs premarket   09:00 ET
    python -m ta.jobs intraday    09:30-16:00 每 5 分钟
    python -m ta.jobs postclose   16:15 ET

每个任务都自己判断"今天是不是交易日"，因为 launchd 只会按钟点触发，
不认识感恩节和独立日。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from ta import store
from ta.alerts import evaluate, evaluate_intraday_rsi, filter_new, render
from ta.config import all_symbols, config
from ta.data.base import DataError
from ta.data.news import AlpacaNews, compile_filters, data_releases, rank
from ta.data.router import DataRouter
from ta.earnings import upcoming as earnings_upcoming
from ta.indicators import compute, rsi, rsi_tiers, rsi_zone
from ta.macro import next_fomc, next_key_events, upcoming
from ta.market import is_market_hours, last_session_close, now_et, session_fraction
from ta.notify.telegram import Telegram
from ta.reports import (Digest, Row, notable_symbols, postclose_report,
                        premarket_report)

log = logging.getLogger("ta.jobs")


def _is_trading_day(router: DataRouter, day: date | None = None) -> bool:
    day = day or now_et().date()
    try:
        return router.alpaca.is_trading_day(day)
    except Exception as exc:
        # 日历接口挂了就退回"非周末即交易日"，宁可多推一次也不要漏掉
        log.warning("交易日历不可用（%s），退回周末判断", exc)
        return day.weekday() < 5


def _collect(router: DataRouter, symbols: list[str]) -> Digest:
    cfg = config()
    frac = session_fraction()
    quotes = router.get_quotes(symbols)
    bars = router.get_daily_bars(symbols, lookback_days=260)

    rows: list[Row] = []
    for sym in symbols:
        series = bars.get(sym)
        snap = compute(sym, series, cfg["indicators"], session_fraction=frac) if series else None
        quote = quotes.get(sym)
        if quote is None and snap is None:
            log.warning("%s 无任何数据，跳过", sym)
            continue
        if series:
            store.save_bars(sym, series[-5:])
        if quote and snap:
            store.save_scan(sym, quote.price, quote.change_pct, snap.rsi,
                            {"trend": snap.trend(), "sma": snap.sma,
                             "volume_ratio": snap.volume_ratio})
        rows.append(Row(symbol=sym, quote=quote, snap=snap))

    bench_syms = cfg.get("benchmarks", [])
    benchmarks = [r for r in rows if r.symbol in bench_syms]
    return Digest(rows=rows, benchmarks=benchmarks)


def _job_enabled(name: str) -> bool:
    """任务开关。关掉的任务 launchd 仍会唤醒，但进程立刻退出——
    比反复装卸 plist 简单，也不会丢掉调度状态。"""
    return bool(config().get("jobs", {}).get(name, True))


def job_premarket(dry_run: bool = False) -> int:
    if not _job_enabled("premarket") and not dry_run:
        log.info("premarket 已在配置中关闭")
        return 0
    router = DataRouter()
    if not _is_trading_day(router):
        log.info("今天非交易日，跳过晨报")
        return 0
    store.init_db()
    digest = _collect(router, all_symbols())
    news, releases = _overnight_news(digest)
    calendar, fomc, lookahead = _macro_calendar()
    text = premarket_report(digest, news=news, releases=releases,
                            calendar=calendar, fomc=fomc, lookahead=lookahead,
                            earnings=_earnings())
    return _deliver(text, dry_run, label="晨报")


def _earnings() -> list:
    """未来若干天内的 watchlist 财报。失败不影响晨报发出。"""
    cfg = config().get("earnings", {})
    if not cfg.get("enabled", True):
        return []
    try:
        return earnings_upcoming(all_symbols(), int(cfg.get("lookahead_days", 14)))
    except Exception as exc:
        log.warning("财报日程获取失败：%s", exc)
        return []


def _macro_calendar() -> tuple[list, object, list]:
    """未来几天的宏观日程 + 下次 FOMC + 窗口外的关键前瞻。
    失败不影响晨报发出。"""
    cfg = config().get("macro", {})
    if not cfg.get("enabled", True):
        return [], None, []
    window = int(cfg.get("lookahead_days", 7))
    try:
        events = upcoming(window, extra=cfg.get("extra_events"))
        return events, next_fomc(), next_key_events(within=window)
    except Exception as exc:
        log.warning("宏观日历获取失败：%s", exc)
        return [], None, []


def _overnight_news(digest: Digest) -> tuple[list, list]:
    """抓隔夜新闻，返回（正文新闻, 数据发布）。

    抓不到就返回空 —— 新闻是锦上添花，不能因为它挂了
    让整份晨报发不出去。"""
    cfg = config().get("news", {})
    if not cfg.get("enabled", True):
        return [], []
    try:
        items = AlpacaNews().fetch(all_symbols(), last_session_close(), limit=60)
    except Exception as exc:
        log.warning("新闻抓取失败，晨报将不含新闻区块：%s", exc)
        return [], []
    news = rank(items, boosted=notable_symbols(digest),
                limit=int(cfg.get("max_items", 10)),
                per_symbol=int(cfg.get("per_symbol", 2)),
                filters=compile_filters(cfg.get("exclude_patterns")))
    return news, data_releases(items, limit=int(cfg.get("max_releases", 4)))


def job_postclose(dry_run: bool = False) -> int:
    if not _job_enabled("postclose") and not dry_run:
        log.info("postclose 已在配置中关闭")
        return 0
    router = DataRouter()
    if not _is_trading_day(router):
        log.info("今天非交易日，跳过盘后")
        return 0
    store.init_db()
    digest = _collect(router, all_symbols())
    text = postclose_report(digest)
    return _deliver(text, dry_run, label="盘后")


def job_intraday(dry_run: bool = False, force: bool = False,
                 summary: bool = False) -> int:
    """盘中轮询。非交易时段直接退出，让 launchd 的固定间隔调度保持简单。"""
    if not _job_enabled("intraday") and not force:
        log.debug("intraday 已在配置中关闭")
        return 0
    router = DataRouter()
    if not force:
        if not is_market_hours():
            log.debug("非交易时段，跳过")
            return 0
        if not _is_trading_day(router):
            log.info("今天非交易日，跳过")
            return 0
    return _run_rsi_check(router, dry_run=dry_run, summary=summary)


def job_check(dry_run: bool = False, force: bool = False) -> int:
    """定点巡检：无论有无信号都回报一次。

    与 intraday 的区别在于把关方式：这里不看是否在交易时段
    （7:00 的盘前巡检本来就在开盘前），但仍然跳过非交易日 ——
    周末和假日推一条"一切正常"只是噪音。
    """
    if not _job_enabled("check") and not force:
        log.debug("check 已在配置中关闭")
        return 0
    router = DataRouter()
    if not force and not _is_trading_day(router):
        log.info("今天非交易日，跳过巡检")
        return 0
    return _run_rsi_check(router, dry_run=dry_run, summary=True)


def _run_rsi_check(router: DataRouter, dry_run: bool, summary: bool) -> int:
    store.init_db()
    digest = _collect(router, all_symbols())

    #  日线信号
    daily = []
    for r in digest.rows:
        if r.quote:
            daily.extend(evaluate(r.quote, r.snap))

    #  分钟线 RSI —— 与日线完全独立的一路信号
    intraday, intraday_rsi = _intraday_rsi_alerts([r.symbol for r in digest.rows])

    sent = 0
    for group, title in ((daily, "日线"), (intraday, "5 分钟线")):
        #  summary 模式是人手动触发的一次性查看，不消耗当日的去重额度，
        #  否则手动查一次会把后面真正的自动告警吞掉
        fresh = group if (dry_run or summary) else filter_new(group)
        if not fresh:
            continue
        #  日线与分钟线分成两条消息推送：时间尺度不同，混在一起
        #  容易把短线噪音当成趋势信号
        header = (f"<b>⚡ {title}信号</b>  {now_et().strftime('%H:%M ET')}\n\n")
        if _deliver(header + render(fresh), dry_run, label=f"{title} {len(fresh)} 条") == 0:
            sent += len(fresh)

    if summary and not sent:
        daily_rsi = {r.symbol: r.snap.rsi for r in digest.rows
                     if r.snap and r.snap.rsi is not None}
        _deliver(_no_signal_report(daily_rsi, intraday_rsi), dry_run, label="无信号回报")
    elif not sent:
        log.info("无新告警（日线候选 %d、分钟线候选 %d，均已推送过）",
                 len(daily), len(intraday))
    return 0


def _fmt_tiers(tiers: list) -> str:
    return "/".join(f"{t:g}" for t in tiers)


def _no_signal_report(daily: dict, intraday: dict) -> str:
    """没有触发时的回报。

    只说一句"没有信号"没法让人确信程序在正常工作，故附上扫描范围、
    RSI 区间，以及离阈值最近的几只——看得见它确实在算。
    """
    cfg = config()["indicators"]["rsi"]
    low, high = rsi_zone(cfg)
    tiers_low, tiers_high = rsi_tiers(cfg)
    label = config()["indicators"].get("intraday", {}).get("label", "分钟")

    lines = [f"<b>✅ RSI 检查完成</b>  {now_et().strftime('%m-%d %H:%M ET')}",
             f"<i>无标的触及 {_fmt_tiers(tiers_low)} / {_fmt_tiers(tiers_high)}</i>", ""]
    if not is_market_hours():
        #  盘前盘后的分钟线是上一交易时段的收尾，不是实时读数，
        #  不标出来容易被误读成当下的盘中状态
        lines.append(f"<i>当前休市，{label}线读数为上一交易时段收尾</i>")
        lines.append("")

    for name, values in ((f"日线", daily), (f"{label}线", intraday)):
        if not values:
            lines.append(f"<b>{name}</b>　无数据")
            continue
        lo_sym, lo_val = min(values.items(), key=lambda kv: kv[1])
        hi_sym, hi_val = max(values.items(), key=lambda kv: kv[1])
        lines.append(f"<b>{name}</b>　{len(values)} 只，区间 {lo_val:.0f}–{hi_val:.0f}")
        lines.append(f"　最低 <code>{lo_sym}</code> {lo_val:.1f}　"
                     f"最高 <code>{hi_sym}</code> {hi_val:.1f}")
    return "\n".join(lines)


def _intraday_rsi_alerts(symbols: list[str]) -> tuple[list, dict]:
    """分钟线 RSI 信号。返回（信号列表, 各标的 RSI 读数）。

    读数一并返回，供"无信号回报"展示扫描范围——只说没有信号
    没法让人确信程序在正常工作。取数失败不影响日线那一路。"""
    cfg = config()["indicators"].get("intraday", {})
    if not cfg.get("enabled", True) or not symbols:
        return [], {}
    timeframe = str(cfg.get("timeframe", "5Min"))
    label = str(cfg.get("label", "5 分钟"))
    period = int(config()["indicators"]["rsi"]["period"])
    try:
        from ta.data.alpaca import AlpacaProvider
        bars = AlpacaProvider().get_intraday_bars(
            symbols, timeframe=timeframe, days=int(cfg.get("lookback_days", 5)))
    except Exception as exc:
        log.warning("分钟线取数失败：%s", exc)
        return [], {}

    out, readings = [], {}
    for sym in symbols:
        series = bars.get(sym) or []
        if len(series) <= period:
            continue
        closes = [b.close for b in series]
        value = rsi(closes, period)[-1]
        if value is not None:
            readings[sym] = value
        out.extend(evaluate_intraday_rsi(sym, value, closes[-1], timeframe=label))
    return out, readings


def _deliver(text: str, dry_run: bool, label: str) -> int:
    if dry_run:
        print(f"--- [dry-run] {label} ---")
        print(text)
        return 0
    try:
        parts = Telegram().send(text)
    except Exception as exc:
        log.error("推送失败：%s", exc)
        return 1
    log.info("%s 已推送（%d 条消息）", label, parts)
    return 0


JOBS = {
    "check": lambda a: job_check(a.dry_run, a.force),
    "premarket": lambda a: job_premarket(a.dry_run),
    "postclose": lambda a: job_postclose(a.dry_run),
    "intraday": lambda a: job_intraday(a.dry_run, a.force, a.summary),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ta.jobs", description="定时任务")
    p.add_argument("job", choices=sorted(JOBS))
    p.add_argument("--dry-run", action="store_true", help="只打印不推送")
    p.add_argument("--force", action="store_true", help="忽略交易时段限制")
    p.add_argument("--summary", action="store_true",
                   help="无论有无信号都推送一条回报（手动查看用，不占当日去重额度）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return JOBS[args.job](args)
    except DataError as exc:
        log.error("数据获取失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
