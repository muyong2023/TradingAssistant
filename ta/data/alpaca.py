"""Alpaca provider —— 负责实时报价与交易日历。

免费档的行情走 IEX feed：价格可用，成交量偏低（只有 IEX 一家撮合的量），
所以 Quote.volume_is_partial 会置 True，量能指标不要用它。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from ta.config import redact, secrets
from ta.data.base import Bar, DataError, Quote

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://api.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
TIMEOUT = 20


class AlpacaProvider:
    name = "alpaca"

    def __init__(self, feed: str = "iex") -> None:
        s = secrets()
        s.require("alpaca_key_id", "alpaca_secret")
        self.feed = feed
        #  /v2/assets 用哪个域名，首次成功后记住
        self._account_host: str | None = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                "APCA-API-KEY-ID": s.alpaca_key_id,
                "APCA-API-SECRET-KEY": s.alpaca_secret,
            }
        )

    def _get(self, base: str, path: str, **params) -> dict:
        try:
            resp = self._session.get(f"{base}{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise DataError(f"alpaca 请求失败 {path}: {redact(exc)}") from exc
        if resp.status_code != 200:
            raise DataError(f"alpaca {path} 返回 {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """用 snapshots 一次拿齐最新成交价、当日 bar 和昨收。"""
        if not symbols:
            return {}
        payload = self._get(
            DATA_URL,
            "/v2/stocks/snapshots",
            symbols=",".join(symbols),
            feed=self.feed,
        )
        # 该端点在有数据时直接以 symbol 为顶层键
        raw = payload.get("snapshots", payload)
        out: dict[str, Quote] = {}
        for sym in symbols:
            snap = raw.get(sym)
            if not isinstance(snap, dict):
                continue
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}
            trade = snap.get("latestTrade") or {}
            price = trade.get("p") or daily.get("c")
            prev_close = prev.get("c")
            if not price or not prev_close:
                continue
            out[sym] = Quote(
                symbol=sym,
                price=float(price),
                prev_close=float(prev_close),
                day_open=_f(daily.get("o")),
                day_high=_f(daily.get("h")),
                day_low=_f(daily.get("l")),
                day_volume=_f(daily.get("v")),
                ts=_parse_ts(trade.get("t")) or datetime.now(timezone.utc),
                source=f"alpaca/{self.feed}",
                volume_is_partial=self.feed == "iex",
            )
        return out

    def get_daily_bars(self, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        out: dict[str, list[Bar]] = {}
        page_token = None
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "limit": 10000,
                "feed": self.feed,
                "adjustment": "split",
            }
            if page_token:
                params["page_token"] = page_token
            payload = self._get(DATA_URL, "/v2/stocks/bars", **params)
            for sym, bars in (payload.get("bars") or {}).items():
                out.setdefault(sym, []).extend(
                    Bar(
                        day=datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date(),
                        open=float(b["o"]),
                        high=float(b["h"]),
                        low=float(b["l"]),
                        close=float(b["c"]),
                        volume=float(b["v"]),
                    )
                    for b in bars
                )
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        for sym in out:
            out[sym] = sorted(out[sym], key=lambda b: b.day)[-lookback_days:]
        return out

    def get_intraday_bars(self, symbols: list[str], timeframe: str = "5Min",
                          days: int = 5,
                          regular_hours_only: bool = True) -> dict[str, list[Bar]]:
        """分钟级 K 线。

        免费档同样走 IEX feed，成交量偏低但价格可用 —— RSI 只吃收盘价，
        不受量的影响。

        regular_hours_only 默认开启：IEX 会返回盘前盘后的成交，那些
        K 线极稀疏（实测 NVDA 08:05 那根只有 140 股），几笔零星成交
        就能把 RSI 拉到极值，产生假信号。
        """
        if not symbols:
            return {}
        start = (datetime.now(timezone.utc) - timedelta(days=days))
        out: dict[str, list[Bar]] = {}
        page_token = None
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start.isoformat(),
                "limit": 10000,
                "feed": self.feed,
                "adjustment": "split",
            }
            if page_token:
                params["page_token"] = page_token
            payload = self._get(DATA_URL, "/v2/stocks/bars", **params)
            for sym, bars in (payload.get("bars") or {}).items():
                for b in bars:
                    ts = _parse_ts(b["t"])
                    if ts is None:
                        continue
                    if regular_hours_only and not _in_regular_session(ts):
                        continue
                    out.setdefault(sym, []).append(Bar(
                        day=ts.date(), open=float(b["o"]), high=float(b["h"]),
                        low=float(b["l"]), close=float(b["c"]), volume=float(b["v"]),
                    ))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return out

    def get_asset(self, symbol: str) -> dict | None:
        """查一个代码是否存在。用于加自选前的校验。

        当初把苹果写成 APPL（正确是 AAPL）就是这样漏过去的 ——
        代码不存在时行情接口只是静默返回空，不会报错。

        /v2/assets 与账户类型绑定：Paper 的 key 在 live 域名上会拿到 401，
        反之亦然。故按需探测一次并记住结果。
        （/v2/calendar 不绑账户，两个域名都可用，无需此处理。）
        """
        path = f"/v2/assets/{symbol.upper()}"
        hosts = [self._account_host] if self._account_host else [TRADING_URL, PAPER_URL]
        for host in hosts:
            try:
                asset = self._get(host, path)
            except DataError as exc:
                if "401" in str(exc) or "403" in str(exc):
                    continue          # 域名与账户类型不匹配，换一个
                return None           # 404 等：代码确实不存在
            self._account_host = host
            return asset
        return None

    def is_trading_day(self, day: date) -> bool:
        """问交易日历，自动排除周末和节假日。"""
        iso = day.isoformat()
        days = self._get(TRADING_URL, "/v2/calendar", start=iso, end=iso)
        return any(d.get("date") == iso for d in days)


_ET = ZoneInfo("America/New_York")
_OPEN, _CLOSE = time(9, 30), time(16, 0)


def _in_regular_session(ts: datetime) -> bool:
    """是否落在 09:30–16:00 ET 的正常交易时段。"""
    local = ts.astimezone(_ET)
    return local.weekday() < 5 and _OPEN <= local.time() < _CLOSE


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        # Alpaca 的纳秒精度时间戳 fromisoformat 处理不了，截到微秒
        text = v.replace("Z", "+00:00")
        if "." in text:
            head, _, tail = text.partition(".")
            digits = "".join(c for c in tail if c.isdigit())[:6].ljust(6, "0")
            offset = tail[len(digits):] if len(tail) > len(digits) else "+00:00"
            offset = offset if offset.startswith(("+", "-")) else "+00:00"
            text = f"{head}.{digits}{offset}"
        return datetime.fromisoformat(text)
    except ValueError:
        return None
