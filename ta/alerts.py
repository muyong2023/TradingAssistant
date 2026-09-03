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
from ta.indicators import Snapshot, rsi_hit, rsi_tiers


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
        hit = rsi_hit(snap.rsi, rsi_cfg)
        if hit:
            direction, tier = hit
            word = "超卖" if direction == "oversold" else "超买"
            side = "低于" if direction == "oversold" else "高于"
            out.append(Alert(
                symbol=quote.symbol, kind="rsi_extreme",
                #  档位写进 tier，两档因而各自去重：
                #  先到 28 报过 30 档后，跌到 18 仍会再报 20 档
                tier=f"{direction}{tier:g}",
                headline=f"RSI {snap.rsi:.1f} {word}   ${quote.price:,.2f}",
                detail=f"{side}阈值 {tier:g}  ·  {snap.trend()}  ·  当日 {change:+.2f}%",
                severity=_rsi_severity(tier, rsi_cfg),
                magnitude=_rsi_magnitude(snap.rsi, direction, tier),
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

def _rsi_severity(tier: float, cfg: dict) -> int:
    """最内侧那一档算重要，外档算一般。"""
    low, high = rsi_tiers(cfg)
    return 2 if tier in (low[-1], high[-1]) else 1


def _rsi_magnitude(value: float, direction: str, tier: float) -> float:
    """超出该档多少，用于同一批告警之间排序。"""
    beyond = (tier - value) if direction == "oversold" else (value - tier)
    return 1.0 + max(beyond, 0.0) / 20.0


def evaluate_intraday_rsi(symbol: str, rsi_value: float | None, price: float,
                          timeframe: str = "5分钟") -> list[Alert]:
    """分钟线 RSI 的超买超卖。

    与日线分开成独立的 kind，因而去重也各自独立 —— 同一只票的日线
    超卖和 5 分钟超卖是两件事，都值得知道。档位处理与日线一致。
    """
    if rsi_value is None:
        return []
    cfg = config()["indicators"]["rsi"]
    hit = rsi_hit(rsi_value, cfg)
    if not hit:
        return []
    direction, tier = hit
    word = "超卖" if direction == "oversold" else "超买"
    side = "低于" if direction == "oversold" else "高于"

    return [Alert(
        symbol=symbol,
        kind="rsi_intraday",
        tier=f"{direction}{tier:g}",
        headline=f"{timeframe} RSI {rsi_value:.1f} {word}   ${price:,.2f}",
        detail=f"{side}阈值 {tier:g}  ·  分组 {group_of(symbol)}",
        severity=_rsi_severity(tier, cfg),
        magnitude=_rsi_magnitude(rsi_value, direction, tier),
    )]
