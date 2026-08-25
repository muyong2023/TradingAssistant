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
from ta.alerts import evaluate, filter_new, render
from ta.config import all_symbols, config
from ta.data.base import DataError
from ta.data.news import AlpacaNews, compile_filters, data_releases, rank
from ta.data.router import DataRouter
from ta.earnings import upcoming as earnings_upcoming
from ta.indicators import compute
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


def job_premarket(dry_run: bool = False) -> int:
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
    router = DataRouter()
    if not _is_trading_day(router):
        log.info("今天非交易日，跳过盘后")
        return 0
    store.init_db()
    digest = _collect(router, all_symbols())
    text = postclose_report(digest)
    return _deliver(text, dry_run, label="盘后")


def job_intraday(dry_run: bool = False, force: bool = False) -> int:
    """盘中轮询。非交易时段直接退出，让 launchd 的固定间隔调度保持简单。"""
    router = DataRouter()
    if not force:
        if not is_market_hours():
            log.debug("非交易时段，跳过")
            return 0
        if not _is_trading_day(router):
            log.info("今天非交易日，跳过")
            return 0
    store.init_db()
    digest = _collect(router, all_symbols())

    candidates = []
    for r in digest.rows:
        if r.quote:
            candidates.extend(evaluate(r.quote, r.snap))
    fresh = filter_new(candidates) if not dry_run else candidates

    if not fresh:
        log.info("无新告警（候选 %d 条，均已推送过）", len(candidates))
        return 0

    header = f"<b>⚡ 盘中异动</b>  {now_et().strftime('%H:%M ET')}\n\n"
    body = header + render(fresh)
    return _deliver(body, dry_run, label=f"{len(fresh)} 条告警")


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
    "premarket": lambda a: job_premarket(a.dry_run),
    "postclose": lambda a: job_postclose(a.dry_run),
    "intraday": lambda a: job_intraday(a.dry_run, a.force),
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ta.jobs", description="定时任务")
    p.add_argument("job", choices=sorted(JOBS))
    p.add_argument("--dry-run", action="store_true", help="只打印不推送")
    p.add_argument("--force", action="store_true", help="忽略交易时段限制")
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
