"""本地看板。

    ./ta.sh web          启动，浏览器打开 http://127.0.0.1:8787

默认只监听回环地址：里面有你的持仓和自选股，没有认证，
不应该暴露到局域网。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote_plus
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ta.config import all_symbols, config, group_of, watchlists
from ta.data.base import DataError
from ta import watchlist
from ta.data.router import DataRouter
from ta.earnings import all_events as earnings_all
from ta.indicators import compute, rsi
from ta.market import is_market_hours, now_et, session_fraction
from ta.reports import Digest, Row
from ta.web import charts
from ta.watchlist import WatchlistError

HERE = Path(__file__).resolve().parent
app = FastAPI(title="交易小助手")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["spark"] = charts.sparkline
templates.env.filters["meter"] = charts.rsi_meter

_router = DataRouter()


DETAIL_WINDOW = 180


def _digest(symbols: list[str], lookback_days: int = 260) -> tuple[Digest, dict[str, list]]:
    cfg = config()
    frac = session_fraction()
    quotes = _router.get_quotes(symbols)
    bars = _router.get_daily_bars(symbols, lookback_days=lookback_days)
    rows = []
    for sym in symbols:
        series = bars.get(sym)
        snap = compute(sym, series, cfg["indicators"], session_fraction=frac) if series else None
        rows.append(Row(symbol=sym, quote=quotes.get(sym), snap=snap))
    bench = [r for r in rows if r.symbol in cfg.get("benchmarks", [])]
    return Digest(rows=rows, benchmarks=bench), bars


def _context(request: Request) -> dict:
    return {
        "request": request,
        "now": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "market_open": is_market_hours(),
        "session_pct": round(session_fraction() * 100),
        "groups_meta": watchlists(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    try:
        digest, bars = _digest(all_symbols())
    except DataError as exc:
        return templates.TemplateResponse(
            request, "error.html", {**_context(request), "message": str(exc)},
            status_code=503,
        )
    gainers, losers = digest.movers(3)
    over, under = digest.rsi_extremes()
    ctx = {
        **_context(request),
        "digest": digest,
        "grouped": digest.by_group(),
        "bars": {s: b[-30:] for s, b in bars.items()},
        "gainers": gainers,
        "losers": losers,
        "over": over,
        "under": under,
        "source": _router.last_quote_source,
        "advancing": sum(1 for r in digest.rows if r.change > 0),
        "declining": sum(1 for r in digest.rows if r.change < 0),
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/s/{symbol}", response_class=HTMLResponse)
def detail(request: Request, symbol: str):
    symbol = symbol.upper()
    if symbol not in all_symbols():
        raise HTTPException(404, f"{symbol} 不在任何 watchlist 中")
    #  显示 180 根 + 200 根均线预热，否则 MA200 只能从图表中段开始画
    try:
        digest, bars = _digest([symbol], lookback_days=DETAIL_WINDOW + 220)
    except DataError as exc:
        return templates.TemplateResponse(
            request, "error.html", {**_context(request), "message": str(exc)},
            status_code=503,
        )
    row = digest.rows[0]
    series = bars.get(symbol, [])
    cfg = config()["indicators"]["rsi"]
    #  传完整序列给 price_chart，让它自己在全量上算均线再截取窗口
    window = series[-DETAIL_WINDOW:]
    rsi_vals = rsi([b.close for b in series], cfg["period"])[-DETAIL_WINDOW:]
    earn = next((e for e in earnings_all(all_symbols()) if e.symbol == symbol), None)
    ctx = {
        **_context(request),
        "row": row,
        "symbol": symbol,
        "earnings": earn,
        "group": group_of(symbol),
        "price_svg": charts.price_chart(series, window=DETAIL_WINDOW),
        "rsi_svg": charts.rsi_chart(window, rsi_vals, cfg["oversold"], cfg["overbought"]),
        "bars": window,
    }
    return templates.TemplateResponse(request, "detail.html", ctx)


def _same_origin(request: Request) -> None:
    """拒绝跨站发来的写请求。

    页面监听在回环地址、没有认证，但浏览器允许任意网站向 127.0.0.1
    提交表单。没有这道检查，你在别处打开的恶意页面就能悄悄改你的自选股。
    """
    origin = request.headers.get("origin")
    if origin and request.url.netloc not in origin:
        raise HTTPException(403, "跨站请求已拒绝")


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, msg: str = "", err: str = ""):
    cfg = config()
    return templates.TemplateResponse(request, "watchlist.html", {
        **_context(request),
        "groups": watchlist.groups(),
        "labels": {k: v["label"] for k, v in cfg["watchlists"].items()},
        "alerts": {k: v["alert"]["pct"] for k, v in cfg["watchlists"].items()},
        "msg": msg,
        "err": err,
    })


@app.post("/watchlist/add")
def watchlist_add(request: Request, symbol: str = Form(...), group: str = Form(...)):
    _same_origin(request)
    symbol = symbol.strip().upper()
    ok, note = watchlist.validate(symbol)
    if not ok:
        return RedirectResponse(f"/watchlist?err={quote_plus(note)}", status_code=303)
    try:
        result = watchlist.add(symbol, group)
    except WatchlistError as exc:
        return RedirectResponse(f"/watchlist?err={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse(f"/watchlist?msg={quote_plus(result + ' · ' + note)}",
                            status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(request: Request, symbol: str = Form(...)):
    _same_origin(request)
    try:
        result = watchlist.remove(symbol)
    except WatchlistError as exc:
        return RedirectResponse(f"/watchlist?err={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse(f"/watchlist?msg={quote_plus(result)}", status_code=303)


@app.post("/watchlist/group/add")
def group_add(request: Request, key: str = Form(...), label: str = Form(""),
              pct_low: float = Form(10), pct_high: float = Form(20)):
    _same_origin(request)
    try:
        result = watchlist.create_group(key, label or key, (pct_low, pct_high))
    except WatchlistError as exc:
        return RedirectResponse(f"/watchlist?err={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse(f"/watchlist?msg={quote_plus(result)}", status_code=303)


@app.post("/watchlist/group/remove")
def group_remove(request: Request, key: str = Form(...), force: str = Form("")):
    _same_origin(request)
    try:
        result = watchlist.delete_group(key, force=bool(force))
    except WatchlistError as exc:
        return RedirectResponse(f"/watchlist?err={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse(f"/watchlist?msg={quote_plus(result)}", status_code=303)


@app.get("/api/snapshot")
def api_snapshot():
    """给外部脚本用的 JSON。"""
    digest, _ = _digest(all_symbols())
    return {
        "ts": datetime.now().isoformat(),
        "market_open": is_market_hours(),
        "rows": [
            {
                "symbol": r.symbol,
                "group": group_of(r.symbol),
                "price": r.price,
                "change_pct": round(r.change, 2),
                "rsi": round(r.snap.rsi, 1) if r.snap and r.snap.rsi is not None else None,
                "trend": r.snap.trend() if r.snap else None,
                "volume_ratio": round(r.snap.volume_ratio, 2)
                if r.snap and r.snap.volume_ratio else None,
            }
            for r in digest.rows
        ],
    }
