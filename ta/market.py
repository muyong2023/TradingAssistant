"""美股交易时段的时间计算。定时任务和盘中量能折算都要用。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
SESSION_MINUTES = 390.0  # 6.5 小时


def now_et() -> datetime:
    return datetime.now(ET)


def is_market_hours(at: datetime | None = None) -> bool:
    at = (at or now_et()).astimezone(ET)
    return at.weekday() < 5 and OPEN <= at.time() < CLOSE


def session_fraction(at: datetime | None = None) -> float:
    """当前交易时段已过去的比例，用于把盘中的部分成交量折算成全日预估。

    返回 1.0 表示收盘后（或非交易时段），此时成交量已经是完整的，
    不需要折算。
    """
    at = (at or now_et()).astimezone(ET)
    if at.weekday() >= 5:
        return 1.0
    if at.time() < OPEN:
        return 1.0        # 盘前：最新 bar 仍是上一交易日的完整数据
    if at.time() >= CLOSE:
        return 1.0
    elapsed = (
        datetime.combine(at.date(), at.time()) - datetime.combine(at.date(), OPEN)
    ) / timedelta(minutes=1)
    return max(elapsed / SESSION_MINUTES, 0.02)   # 开盘头几分钟别放大到离谱


def latest_bar_is_today(latest_day: date, at: datetime | None = None) -> bool:
    return latest_day == (at or now_et()).astimezone(ET).date()


def last_session_close(at: datetime | None = None) -> datetime:
    """上一个交易时段的收盘时刻（ET）。

    "隔夜新闻"的起点。不能简单地减去固定小时数：周一 09:00 的隔夜区间
    要一直回溯到上周五 16:00，跨了 65 小时。

    只按工作日判断，不查节假日 —— 遇到假期会多覆盖一天的新闻，
    宁可多给也不要漏掉假期期间积压的消息。
    """
    at = (at or now_et()).astimezone(ET)
    day = at.date()
    #  当天已收盘则收盘点就是今天，否则从前一天开始往回找工作日
    if not (at.weekday() < 5 and at.time() >= CLOSE):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, CLOSE, tzinfo=ET)
