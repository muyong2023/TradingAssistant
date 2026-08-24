"""数据层的统一类型与接口。

两个 provider 各有所长，故意不做成"可互换的等价物"：
- Alpaca 免费档走 IEX feed，价格准，但成交量只是 IEX 一家的量，
  远小于全市场合并量，不能拿来算量能指标。
- yfinance 给的是合并量和完整的财报/估值，但延迟约 15 分钟。
Router 依此分工，而不是简单地"谁先成功用谁"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    """一根 K 线。"""
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    """一个标的的当前状态快照。"""
    symbol: str
    price: float
    prev_close: float
    day_open: float | None
    day_high: float | None
    day_low: float | None
    day_volume: float | None
    ts: datetime
    source: str
    #  IEX-only 的量不可用于量能判断，标记出来避免误用
    volume_is_partial: bool = False

    @property
    def change(self) -> float:
        return self.price - self.prev_close

    @property
    def change_pct(self) -> float:
        if not self.prev_close:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0


class QuoteProvider(Protocol):
    name: str

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        ...


class HistoryProvider(Protocol):
    name: str

    def get_daily_bars(self, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
        ...


class DataError(RuntimeError):
    """provider 层的可恢复错误，触发 router 降级。"""
