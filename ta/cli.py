"""命令行入口。

    ta scan            扫描全部 watchlist
    ta scan -g etf     只扫某一组
    ta scan NVDA KO    只扫指定标的
"""
from __future__ import annotations

import argparse
import logging
import sys

from ta import store
from ta.config import all_symbols, config, group_of, watchlists
from ta.data.base import DataError
from ta.data.router import DataRouter
from ta.indicators import compute
from ta.market import is_market_hours, session_fraction

# 终端色彩：涨绿跌红，无 tty 时自动关闭
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _pct(v: float, width: int) -> str:
    """先补足宽度再上色，否则 ANSI 转义符会被算进列宽把表格挤歪。"""
    text = f"{v:+.2f}%".rjust(width)
    if v >= 2:
        return _c(text, "32")
    if v <= -2:
        return _c(text, "31")
    return text


def _rsi_cell(v: float | None, cfg: dict, width: int) -> str:
    if v is None:
        return "-".rjust(width)
    text = f"{v:.1f}".rjust(width)
    if v <= cfg["oversold"]:
        return _c(text, "36")      # 超卖，青色
    if v >= cfg["overbought"]:
        return _c(text, "35")      # 超买，紫色
    return text


def cmd_scan(args) -> int:
    cfg = config()
    ind_cfg = cfg["indicators"]
    rsi_cfg = ind_cfg["rsi"]

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.group:
        groups = watchlists()
        if args.group not in groups:
            print(f"没有这个组：{args.group}。可选：{', '.join(groups)}", file=sys.stderr)
            return 2
        symbols = groups[args.group]["symbols"]
    else:
        symbols = all_symbols()

    router = DataRouter()
    store.init_db()
    frac = session_fraction()

    try:
        quotes = router.get_quotes(symbols)
    except DataError as exc:
        print(f"取报价失败：{exc}", file=sys.stderr)
        return 1
    try:
        bars = router.get_daily_bars(symbols, lookback_days=260)
    except DataError as exc:
        print(f"取历史失败：{exc}", file=sys.stderr)
        return 1

    rows = []
    for sym in symbols:
        series = bars.get(sym)
        quote = quotes.get(sym)
        if not series:
            rows.append((sym, None, None, "无历史数据"))
            continue
        snap = compute(sym, series, ind_cfg, session_fraction=frac)
        if snap is None:
            rows.append((sym, None, None, "指标计算失败"))
            continue
        store.save_bars(sym, series[-5:])
        change = quote.change_pct if quote else 0.0
        price = quote.price if quote else snap.close
        store.save_scan(sym, price, change, snap.rsi, {
            "sma": snap.sma, "rsi": snap.rsi, "trend": snap.trend(),
            "volume_ratio": snap.volume_ratio,
        })
        rows.append((sym, quote, snap, None))

    _print_table(rows, rsi_cfg, router)
    return 0


def _print_table(rows, rsi_cfg, router) -> None:
    header = (
        f"{'标的':<6} {'组':<11} {'现价':>9} {'涨跌':>9} "
        f"{'RSI':>6} {'vs SMA20':>9} {'vs SMA50':>9} {'vs SMA200':>10} "
        f"{'量比':>7}  趋势"
    )
    print(header)
    print("-" * 104)

    notable = []
    for sym, quote, snap, err in rows:
        if err:
            print(f"{sym:<6} {group_of(sym):<11} {err}")
            continue
        price = quote.price if quote else snap.close
        change = quote.change_pct if quote else 0.0
        g20, g50, g200 = (snap.sma_gap_pct.get(p) for p in (20, 50, 200))
        vr = snap.volume_ratio
        print(
            f"{sym:<6} {group_of(sym):<11} {price:>9.2f} {_pct(change, 9)} "
            f"{_rsi_cell(snap.rsi, rsi_cfg, 6)} "
            f"{_fmt_gap(g20):>9} {_fmt_gap(g50):>9} {_fmt_gap(g200):>10} "
            f"{_fmt_vr(vr, snap.volume_ratio_projected):>7}  {snap.trend()}"
        )
        if snap.rsi is not None:
            if snap.rsi <= rsi_cfg["oversold"]:
                notable.append(f"{sym} RSI {snap.rsi:.1f} 超卖")
            elif snap.rsi >= rsi_cfg["overbought"]:
                notable.append(f"{sym} RSI {snap.rsi:.1f} 超买")
        if abs(change) >= 5:
            notable.append(f"{sym} 当日 {change:+.1f}%")

    print("-" * 104)
    source = router.last_quote_source
    note = "" if source == "alpaca" else "  (已降级，价格有约 15 分钟延迟)"
    print(f"报价源: {source}{note}   共 {len(rows)} 个标的")
    if is_market_hours():
        pct_done = session_fraction() * 100
        print(f"盘中（今日时段已过 {pct_done:.0f}%）—— 量比带 * 为按时段折算的全日预估")
    if notable:
        print("\n值得注意：")
        for item in notable:
            print(f"  • {item}")


def _fmt_gap(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "-"


def _fmt_vr(v: float | None, projected: bool) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}x" + ("*" if projected else "")


def cmd_list(args) -> int:
    import re
    from ta import watchlist
    text = re.sub(r"<[^>]+>", "", watchlist.summary())
    print(text)
    return 0


def cmd_add(args) -> int:
    from ta import watchlist
    from ta.watchlist import WatchlistError
    group = args.group or list(watchlists())[0]
    try:
        print(watchlist.add(args.symbol, group))
    except WatchlistError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_remove(args) -> int:
    from ta import watchlist
    from ta.watchlist import WatchlistError
    try:
        print(watchlist.remove(args.symbol))
    except WatchlistError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ta", description="交易小助手")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="扫描 watchlist 的技术面状态")
    scan.add_argument("symbols", nargs="*", help="指定标的；不填则扫全部")
    scan.add_argument("-g", "--group", help="只扫某一组")
    scan.add_argument("-v", "--verbose", action="store_true")
    scan.set_defaults(func=cmd_scan)

    sub.add_parser("list", help="列出自选股分组").set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="加入自选股")
    add.add_argument("symbol")
    add.add_argument("-g", "--group", help="目标分组，默认第一组")
    add.set_defaults(func=cmd_add)

    rm = sub.add_parser("remove", help="移除自选股")
    rm.add_argument("symbol")
    rm.set_defaults(func=cmd_remove)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
