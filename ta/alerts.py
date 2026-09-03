"""盘中告警规则。

设计要点：
- 阈值按 watchlist 分组取（防御股 ±5%，高波动股 ±15%），
  统一阈值会导致"该响的不响、不该响的天天响"。
- 分档触发：跌破 7% 推一次，继续跌破 12% 再推一次，
  但同一档当天不重复推（去重落在 store.alerts 表）。
- RSI 极值每天每个方向只推一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ta import store
from ta.config import alert_tiers, config, group_of
from ta.data.base import Quote
from ta.indicators import Snapshot


@dataclass(frozen=True)
class Alert:
    symbol: str
    kind: str          # pct_move / rsi_extreme / volume_spike
    tier: str
    headline: str
    detail: str
    severity: int      # 1 = 一般, 2 = 重要
    #  排序用的强度：越大越靠前。单位不同的告警之间靠它比较，
    #  统一口径是"超出该标的自身阈值多少倍"。
    magnitude: float = 0.0

    def line(self) -> str:
        icon = "🔴" if self.severity >= 2 else "🟡"
        return f"{icon} {self.headline}\n<i>{self.detail}</i>"


def format_group(symbol: str, alerts: list[Alert]) -> str:
    """把同一标的的多条告警合并成一个块，避免同名条目散落在推送各处。"""
    ordered = sorted(alerts, key=lambda a: (-a.severity, -a.magnitude))
    body = "\n".join(a.line() for a in ordered)
    return f"<b>{symbol}</b>\n{body}"


def render(alerts: list[Alert]) -> str:
    """按标的分组渲染；标的之间按其最强的一条告警排序。"""
    grouped: dict[str, list[Alert]] = {}
    for a in alerts:
        grouped.setdefault(a.symbol, []).append(a)

    def rank(item):
        _, group = item
        return (-max(a.severity for a in group), -max(a.magnitude for a in group))

    return "\n\n".join(
        format_group(sym, group) for sym, group in sorted(grouped.items(), key=rank)
    )


def evaluate(quote: Quote, snap: Snapshot | None) -> list[Alert]:
    """算出这个标的当前触发了哪些告警（尚未去重）。"""
    out: list[Alert] = []
    cfg = config()
    change = quote.change_pct

    # --- 涨跌幅分档 ---
    tiers = alert_tiers(quote.symbol) if cfg["alerts"].get("pct_move_alert", True) else []
    hit = [t for t in tiers if abs(change) >= t]
    if hit:
        tier = max(hit)                      # 只报触发的最高档
        direction = "涨" if change > 0 else "跌"
        base = min(tiers)
        ratio = abs(change) / base if base else 1.0
        #  三条任一满足即为重要。只用"相对自身分组阈值的倍数"是不够的：
        #  high_vol 组首档就有 15%，跌 19.5% 才 1.3 倍，可双位数跌幅
        #  在任何分组里都是大事，所以再加一条 10% 的绝对下限。
        important = (tier == max(tiers)) or ratio >= 1.5 or abs(change) >= 10.0
        out.append(Alert(
            symbol=quote.symbol,
            kind="pct_move",
            tier=f"{tier:g}{'up' if change > 0 else 'down'}",
            headline=f"当日{direction} {abs(change):.2f}%   ${quote.price:,.2f}",
            detail=(f"昨收 ${quote.prev_close:,.2f}  ·  "
                    f"{group_of(quote.symbol)} 组阈值 ±{tier:g}%"),
            severity=2 if important else 1,
            magnitude=ratio,
        ))

    # --- RSI 极值 ---
    rsi_cfg = cfg["indicators"]["rsi"]
    if cfg["alerts"].get("rsi_alert", True) and snap and snap.rsi is not None:
        low, high = rsi_cfg["oversold"], rsi_cfg["overbought"]
        if snap.rsi <= low:
            #  越深入极值区越重要；刚好压线的读数不该盖过一次暴跌
            beyond = low - snap.rsi
            out.append(Alert(
                symbol=quote.symbol, kind="rsi_extreme", tier="oversold",
                headline=f"RSI {snap.rsi:.1f} 超卖   ${quote.price:,.2f}",
                detail=f"低于阈值 {low}  ·  {snap.trend()}  ·  当日 {change:+.2f}%",
                severity=2 if beyond >= 5 else 1,
                magnitude=1.0 + beyond / 20.0,
            ))
        elif snap.rsi >= high:
            beyond = snap.rsi - high
            out.append(Alert(
                symbol=quote.symbol, kind="rsi_extreme", tier="overbought",
                headline=f"RSI {snap.rsi:.1f} 超买   ${quote.price:,.2f}",
                detail=f"高于阈值 {high}  ·  {snap.trend()}  ·  当日 {change:+.2f}%",
                severity=2 if beyond >= 5 else 1,
                magnitude=1.0 + beyond / 20.0,
            ))

    # --- 放量（默认关闭，噪音大）---
    vol_cfg = cfg["indicators"]["volume"]
    if cfg["alerts"].get("volume_spike_alert", False) and snap and snap.volume_ratio:
        if snap.volume_ratio >= vol_cfg["spike_ratio"]:
            suffix = "（盘中折算预估）" if snap.volume_ratio_projected else ""
            out.append(Alert(
                symbol=quote.symbol, kind="volume_spike", tier="spike",
                headline=f"放量 {snap.volume_ratio:.1f}x{suffix}",
                detail=f"当日 {change:+.2f}%  ·  ${quote.price:,.2f}",
                severity=1,
                magnitude=snap.volume_ratio / vol_cfg["spike_ratio"],
            ))
    return out


def filter_new(alerts: list[Alert], day: date | None = None) -> list[Alert]:
    """去掉今天已经推过的，并把留下的登记进去。

    登记和过滤必须是同一个原子动作，否则并发的两次轮询会重复推送。
    """
    fresh: list[Alert] = []
    for alert in alerts:
        if store.record_alert(alert.symbol, alert.kind, alert.tier,
                              detail=alert.headline, day=day):
            fresh.append(alert)
    return fresh


# --------------------------------------------------------------------------
# 分钟线 RSI
# --------------------------------------------------------------------------

def evaluate_intraday_rsi(symbol: str, rsi_value: float | None, price: float,
                          timeframe: str = "5分钟") -> list[Alert]:
    """分钟线 RSI 的超买超卖。

    与日线分开成独立的 kind，因而去重也各自独立 —— 同一只票的日线
    超卖和 5 分钟超卖是两件事，都值得知道。
    """
    if rsi_value is None:
        return []
    cfg = config()["indicators"]["rsi"]
    low, high = cfg["oversold"], cfg["overbought"]

    if rsi_value <= low:
        beyond, tier, word = low - rsi_value, "oversold", "超卖"
    elif rsi_value >= high:
        beyond, tier, word = rsi_value - high, "overbought", "超买"
    else:
        return []

    return [Alert(
        symbol=symbol,
        kind="rsi_intraday",
        tier=tier,
        headline=f"{timeframe} RSI {rsi_value:.1f} {word}   ${price:,.2f}",
        detail=f"阈值 {low}/{high}  ·  分组 {group_of(symbol)}",
        severity=2 if beyond >= 5 else 1,
        magnitude=1.0 + beyond / 20.0,
    )]
