"""财报日程。

数据来自 yfinance（已在用，无需额外凭据）。逐只查询约 0.16 秒，
36 只近 6 秒，故按日缓存 —— 财报日期一天内不会变。

yfinance 的 Earnings Date 可能返回一个日期或两个：两个表示这是
估计区间（公司尚未正式公告具体日期），此时标注为"预计"。
"""
from __future__ import annotations

import json
import logging
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from ta.config import ROOT
from ta.market import ET

log = logging.getLogger(__name__)

CACHE_PATH = ROOT / "data" / "earnings_cache.json"
CACHE_TTL_DAYS = 1


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    day: date
    #  区间结束日；与 day 相同则表示是确定的单日
    day_end: date | None = None
    eps_estimate: float | None = None
    revenue_estimate: float | None = None

    @property
    def confirmed(self) -> bool:
        return self.day_end is None or self.day_end == self.day

    def label(self) -> str:
        parts = [self.symbol]
        if self.eps_estimate is not None:
            parts.append(f"EPS 预期 ${self.eps_estimate:,.2f}")
        if self.revenue_estimate:
            parts.append(f"营收 ${self.revenue_estimate / 1e9:,.1f}B")
        text = "　".join(parts)
        return text if self.confirmed else f"{text}（日期未定）"


def _fetch_one(symbol: str) -> EarningsEvent | None:
    import yfinance as yf

    #  ETF 没有财报，yfinance 会把 404 直接打到 stderr，
    #  定时任务的日志会被这些无意义的报错刷屏
    with warnings.catch_warnings(), _quiet_yfinance():
        warnings.simplefilter("ignore")
        cal = yf.Ticker(symbol).calendar or {}
    dates = cal.get("Earnings Date") or []
    if not dates:
        return None
    days = sorted(d for d in dates if isinstance(d, date))
    if not days:
        return None
    return EarningsEvent(
        symbol=symbol,
        day=days[0],
        day_end=days[-1] if len(days) > 1 else None,
        eps_estimate=_num(cal.get("Earnings Average")),
        revenue_estimate=_num(cal.get("Revenue Average")),
    )


@contextmanager
def _quiet_yfinance():
    """临时压低 yfinance 的日志级别。"""
    names = ["yfinance", "yfinance.data", "yfinance.scrapers.quote"]
    saved = {}
    for name in names:
        lg = logging.getLogger(name)
        saved[name] = lg.level
        lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch_all(symbols: list[str]) -> list[EarningsEvent]:
    """逐只抓取。单只失败不影响其余 —— 新上市或冷门标的常缺这项数据。"""
    out: list[EarningsEvent] = []
    for sym in symbols:
        try:
            event = _fetch_one(sym)
        except Exception as exc:
            log.debug("%s 财报日期获取失败：%s", sym, exc)
            continue
        if event:
            out.append(event)
    return out


def _read_cache() -> list[EarningsEvent] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(CACHE_PATH.read_text())
        if date.today() - date.fromisoformat(raw["fetched"]) > timedelta(days=CACHE_TTL_DAYS):
            return None
        return [_from_json(e) for e in raw["events"]]
    except Exception as exc:
        log.warning("财报缓存不可用：%s", exc)
        return None


def _from_json(e: dict) -> EarningsEvent:
    return EarningsEvent(
        symbol=e["symbol"],
        day=date.fromisoformat(e["day"]),
        day_end=date.fromisoformat(e["day_end"]) if e.get("day_end") else None,
        eps_estimate=e.get("eps"),
        revenue_estimate=e.get("revenue"),
    )


def _write_cache(events: list[EarningsEvent]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "fetched": date.today().isoformat(),
        "events": [{"symbol": e.symbol, "day": e.day.isoformat(),
                    "day_end": e.day_end.isoformat() if e.day_end else None,
                    "eps": e.eps_estimate, "revenue": e.revenue_estimate}
                   for e in events],
    }, ensure_ascii=False, indent=2))


def all_events(symbols: list[str], force: bool = False) -> list[EarningsEvent]:
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached
    events = fetch_all(symbols)
    if events:
        _write_cache(events)
    elif CACHE_PATH.exists():
        #  全部抓取失败时沿用旧缓存，好过完全没有
        try:
            raw = json.loads(CACHE_PATH.read_text())
            return [_from_json(e) for e in raw["events"]]
        except Exception:
            pass
    return events


def upcoming(symbols: list[str], days: int = 14,
             today: date | None = None) -> list[EarningsEvent]:
    today = today or datetime.now(ET).date()
    end = today + timedelta(days=days)
    return sorted(
        (e for e in all_events(symbols) if today <= e.day <= end),
        key=lambda e: (e.day, e.symbol),
    )
