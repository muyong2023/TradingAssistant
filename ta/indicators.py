"""技术指标。全部是纯函数：输入序列，输出序列或标量，不碰 IO。

约定：输入按时间正序（最早在前），返回值与输入等长，
数据不足的位置为 None —— 这样下标始终对得上原始 bar 序列。
"""
from __future__ import annotations

from dataclasses import dataclass

from ta.data.base import Bar

Series = list[float | None]


def sma(values: list[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period 必须为正")
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: list[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period 必须为正")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    # 用前 period 个值的简单均值做种子，避免头部剧烈失真
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """Wilder 平滑的 RSI，与主流看盘软件一致。"""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def volume_ratio(volumes: list[float], lookback: int = 20,
                 session_fraction: float = 1.0) -> float | None:
    """最新一日量 / 之前 lookback 日均量。>2 通常认为是放量。

    session_fraction < 1 表示当日尚未收盘，最新 bar 只累积了部分成交量。
    直接相比会系统性低估（盘中每只票的量比都会小于 1），
    故按已过去的时段比例折算成全日预估量。
    """
    if len(volumes) < lookback + 1:
        return None
    baseline = volumes[-lookback - 1 : -1]
    avg = sum(baseline) / len(baseline)
    if avg <= 0:
        return None
    latest = volumes[-1]
    if 0 < session_fraction < 1:
        latest = latest / session_fraction
    return latest / avg


@dataclass(frozen=True)
class Snapshot:
    """一个标的最新一根 bar 上的全部指标读数。"""
    symbol: str
    close: float
    sma: dict[int, float | None]
    ema: dict[int, float | None]
    rsi: float | None
    volume_ratio: float | None
    #  盘中折算出来的预估值，收盘后为 False
    volume_ratio_projected: bool
    #  收盘价相对各均线的偏离百分比，正数为在均线上方
    sma_gap_pct: dict[int, float | None]

    def trend(self) -> str:
        """综合"价格在几条均线之上"和"50/200 是否金叉"给趋势标签。

        只看 50/200 的相对位置会误判：一只刚从底部拉起、价格已站上全部
        均线但 50 日线尚未上穿 200 日线的票，会被标成"偏空"，与实际相反。
        """
        s20, s50, s200 = (self.sma.get(p) for p in (20, 50, 200))
        if s50 is None or s200 is None:
            return "数据不足"
        mas = [m for m in (s20, s50, s200) if m is not None]
        above = sum(self.close > m for m in mas)
        golden = s50 > s200

        if above == len(mas):
            return "多头排列" if golden else "底部反转中"
        if above == 0:
            return "空头排列" if not golden else "高位回落"
        return "偏多震荡" if golden else "偏空震荡"


def compute(symbol: str, bars: list[Bar], cfg: dict,
            session_fraction: float = 1.0) -> Snapshot | None:
    """按配置里的周期算出一个标的的完整快照。"""
    if not bars:
        return None
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]

    sma_periods = cfg.get("sma", [20, 50, 200])
    ema_periods = cfg.get("ema", [12, 26])
    rsi_period = int(cfg.get("rsi", {}).get("period", 14))
    vol_lookback = int(cfg.get("volume", {}).get("lookback", 20))

    sma_vals = {p: sma(closes, p)[-1] for p in sma_periods}
    ema_vals = {p: ema(closes, p)[-1] for p in ema_periods}
    close = closes[-1]

    return Snapshot(
        symbol=symbol,
        close=close,
        sma=sma_vals,
        ema=ema_vals,
        rsi=rsi(closes, rsi_period)[-1],
        volume_ratio=volume_ratio(volumes, vol_lookback, session_fraction),
        volume_ratio_projected=session_fraction < 1,
        sma_gap_pct={
            p: ((close - v) / v * 100.0 if v else None) for p, v in sma_vals.items()
        },
    )
