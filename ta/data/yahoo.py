"""yfinance provider —— 负责历史日线（含全市场合并成交量）与基本面。

行情延迟约 15 分钟，不适合盘中告警；但成交量是合并量，
财报/估值数据也齐全，是指标计算和基本面分析的主力。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from ta.data.base import Bar, DataError, Quote


class YahooProvider:
    name = "yahoo"

    def get_daily_bars(self, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        # 指标需要 200 日均线，日历日要比交易日多留 ~40% 的余量
        period_days = max(int(lookback_days * 1.5) + 10, 30)
        try:
            df = yf.download(
                tickers=symbols,
                period=f"{period_days}d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            raise DataError(f"yfinance 下载失败: {exc}") from exc
        if df is None or df.empty:
            raise DataError("yfinance 返回空数据")

        out: dict[str, list[Bar]] = {}
        for sym in symbols:
            try:
                sub = df[sym] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            bars = _frame_to_bars(sub)
            if bars:
                out[sym] = bars[-lookback_days:]
        if not out:
            raise DataError("yfinance 未返回任何可用标的")
        return out

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """降级路径：从最近两根日线推算涨跌幅。"""
        bars = self.get_daily_bars(symbols, lookback_days=2)
        out: dict[str, Quote] = {}
        for sym, series in bars.items():
            if len(series) < 2:
                continue
            today, prev = series[-1], series[-2]
            out[sym] = Quote(
                symbol=sym,
                price=today.close,
                prev_close=prev.close,
                day_open=today.open,
                day_high=today.high,
                day_low=today.low,
                day_volume=today.volume,
                ts=datetime.now(timezone.utc),
                source="yahoo",
                volume_is_partial=False,
            )
        return out


def _frame_to_bars(sub: pd.DataFrame) -> list[Bar]:
    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(sub.columns):
        return []
    sub = sub.dropna(subset=["Close"])
    bars: list[Bar] = []
    for idx, row in sub.iterrows():
        bars.append(
            Bar(
                day=idx.date() if hasattr(idx, "date") else idx,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"] or 0),
            )
        )
    return bars
