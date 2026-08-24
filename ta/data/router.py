"""按用途路由到合适的 provider，并在主源失败时降级。

分工原则（见 base.py 的说明）：
- 报价 -> Alpaca（实时），失败降级 yahoo（15 分钟延迟，会标注出来）
- 历史 -> yahoo（合并成交量），失败降级 Alpaca（量偏低，量能指标会失真）
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ta.config import config
from ta.data.base import Bar, DataError, Quote

log = logging.getLogger(__name__)


class _TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key):
        hit = self._store.get(key)
        if not hit:
            return None
        ts, value = hit
        if time.monotonic() - ts > self.ttl:
            del self._store[key]
            return None
        return value

    def put(self, key, value) -> None:
        self._store[key] = (time.monotonic(), value)


class DataRouter:
    def __init__(self) -> None:
        cfg = config()["data"]
        self.fallback_enabled = bool(cfg.get("fallback", True))
        self._quote_cache = _TTLCache(float(cfg.get("cache_ttl_seconds", 60)))
        self._bars_cache = _TTLCache(600.0)
        self._alpaca = None
        self._yahoo = None
        self.last_quote_source = "-"

    # provider 惰性初始化：没配 Alpaca key 时也能只用 yahoo 跑起来
    @property
    def alpaca(self):
        if self._alpaca is None:
            from ta.data.alpaca import AlpacaProvider
            self._alpaca = AlpacaProvider()
        return self._alpaca

    @property
    def yahoo(self):
        if self._yahoo is None:
            from ta.data.yahoo import YahooProvider
            self._yahoo = YahooProvider()
        return self._yahoo

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        key = ("quotes", tuple(sorted(symbols)))
        cached = self._quote_cache.get(key)
        if cached is not None:
            return cached
        result = self._try_chain(
            "报价",
            [("alpaca", lambda: self.alpaca.get_quotes(symbols)),
             ("yahoo", lambda: self.yahoo.get_quotes(symbols))],
        )
        self._quote_cache.put(key, result)
        return result

    def get_daily_bars(self, symbols: list[str], lookback_days: int = 260) -> dict[str, list[Bar]]:
        key = ("bars", tuple(sorted(symbols)), lookback_days)
        cached = self._bars_cache.get(key)
        if cached is not None:
            return cached
        result = self._try_chain(
            "历史",
            [("yahoo", lambda: self.yahoo.get_daily_bars(symbols, lookback_days)),
             ("alpaca", lambda: self.alpaca.get_daily_bars(symbols, lookback_days))],
        )
        self._bars_cache.put(key, result)
        return result

    def _try_chain(self, what: str, chain) -> dict:
        errors = []
        for name, call in chain:
            try:
                result = call()
            except (DataError, Exception) as exc:
                errors.append(f"{name}: {exc}")
                log.warning("%s 源 %s 失败：%s", what, name, exc)
                if not self.fallback_enabled:
                    break
                continue
            if result:
                if what == "报价":
                    self.last_quote_source = name
                if errors:
                    log.warning("%s 已降级到 %s", what, name)
                return result
            errors.append(f"{name}: 返回空")
            if not self.fallback_enabled:
                break
        raise DataError(f"{what}数据全部源失败 -> " + " | ".join(errors))
