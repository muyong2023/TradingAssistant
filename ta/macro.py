"""宏观日历与核心数据发布的识别。

三个来源，可靠性不同，界面上会区分标注：

1. FOMC 会议 —— 抓美联储官网的官方日历，确定日期。抓不到就用本地缓存。
2. 周度/月度规律发布 —— 初请失业金每周四、非农就业每月第一个周五，
   由规则推导。非农偶有挪期（假期或参考周特殊），故标注为"预计"。
3. CPI / PPI / PCE 的具体日期 —— **不做预测**。BLS 拒绝程序抓取，
   而凭记忆写死日期一旦出错，会让你以为明天有 CPI 却没有，
   比不提供更糟。这类数据改为在实际发布后从新闻流里识别并置顶。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import requests

from ta.config import ROOT
from ta.market import ET

log = logging.getLogger(__name__)

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
CACHE_PATH = ROOT / "data" / "fomc_cache.json"
CACHE_TTL_DAYS = 7
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


@dataclass(frozen=True)
class MacroEvent:
    day: date
    name: str
    detail: str = ""
    #  官方日历为 True；规则推导的为 False，界面上标"预计"
    confirmed: bool = True
    at: time | None = None

    def label(self) -> str:
        parts = [self.name]
        if self.detail:
            parts.append(self.detail)
        text = " ".join(parts)
        return text if self.confirmed else f"{text}（预计）"


# --------------------------------------------------------------------------
# FOMC：官方日历
# --------------------------------------------------------------------------

_ROW = re.compile(
    r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)\s*(?:</strong>)?\s*</div>.*?'
    r'fomc-meeting__date[^>]*>\s*(?:<strong>)?\s*([0-9\-–\s*]+?)\s*(?:</strong>)?\s*</div>',
    re.S)


def parse_fomc(html: str) -> list[MacroEvent]:
    """解析美联储日历页。

    日期形如 "27-28" 或 "17-18*"，星号表示该次会议附带经济预测摘要
    （SEP）与主席记者会 —— 市场关注度显著更高，值得标出来。
    跨月会议的月份写作 "April/May"，取后一个月配对结束日。
    """
    events: list[MacroEvent] = []
    blocks = re.split(r"(20\d{2}) FOMC Meetings", html)
    for i in range(1, len(blocks) - 1, 2):
        year = int(blocks[i])
        seg = blocks[i + 1]
        nxt = re.search(r"20\d{2} FOMC Meetings", seg)
        if nxt:
            seg = seg[: nxt.start()]
        for month_text, day_text in _ROW.findall(seg):
            has_sep = "*" in day_text
            nums = re.findall(r"\d+", day_text)
            if not nums:
                continue
            end_day = int(nums[-1])
            months = [m for m in month_text.split("/") if m in MONTHS]
            if not months:
                continue
            month = MONTHS[months[-1]]
            try:
                when = date(year, month, end_day)
            except ValueError:
                continue
            detail = "利率决议 + 经济预测/记者会" if has_sep else "利率决议"
            events.append(MacroEvent(day=when, name="FOMC 会议结束",
                                     detail=detail, at=time(14, 0)))
    return sorted(events, key=lambda e: e.day)


def _read_cache() -> list[MacroEvent] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(CACHE_PATH.read_text())
        fetched = date.fromisoformat(raw["fetched"])
        if date.today() - fetched > timedelta(days=CACHE_TTL_DAYS):
            return None
        return [MacroEvent(day=date.fromisoformat(e["day"]), name=e["name"],
                           detail=e["detail"], confirmed=True,
                           at=time(14, 0)) for e in raw["events"]]
    except Exception as exc:
        log.warning("FOMC 缓存不可用：%s", exc)
        return None


def _write_cache(events: list[MacroEvent]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "fetched": date.today().isoformat(),
        "events": [{"day": e.day.isoformat(), "name": e.name, "detail": e.detail}
                   for e in events],
    }, ensure_ascii=False, indent=2))


def fomc_meetings(force: bool = False) -> list[MacroEvent]:
    """取 FOMC 日程。优先用缓存（7 天有效），抓取失败则沿用旧缓存。"""
    if not force:
        cached = _read_cache()
        if cached:
            return cached
    try:
        resp = requests.get(FOMC_URL, timeout=20, headers={"User-Agent": UA})
        resp.raise_for_status()
        events = parse_fomc(resp.text)
        if events:
            _write_cache(events)
            return events
        log.warning("FOMC 页面解析出 0 场会议，页面结构可能已变更")
    except Exception as exc:
        log.warning("FOMC 日历抓取失败：%s", exc)
    #  抓取失败时，即使缓存过期也比没有强
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text())
            return [MacroEvent(day=date.fromisoformat(e["day"]), name=e["name"],
                               detail=e["detail"], at=time(14, 0))
                    for e in raw["events"]]
        except Exception:
            pass
    return []


# --------------------------------------------------------------------------
# 规律性发布：由规则推导
# --------------------------------------------------------------------------

def recurring_events(start: date, end: date) -> list[MacroEvent]:
    """区间内的周度/月度固定发布。

    初请失业金：每周四 08:30，规律极稳定。
    非农就业（Employment Situation）：每月第一个周五 08:30。
    偶因假期或参考周特殊而挪期，故标为"预计"。
    """
    events: list[MacroEvent] = []
    day = start
    while day <= end:
        if day.weekday() == 3:      # 周四
            events.append(MacroEvent(day=day, name="初请失业金", detail="每周",
                                     confirmed=True, at=time(8, 30)))
        if day.weekday() == 4 and day.day <= 7:   # 当月第一个周五
            events.append(MacroEvent(day=day, name="非农就业报告",
                                     detail="失业率 / 新增就业",
                                     confirmed=False, at=time(8, 30)))
        day += timedelta(days=1)
    return events


def parse_extra(entries: list[dict] | None) -> list[MacroEvent]:
    """配置里手填的事件（CPI / PCE / PPI 等 BLS 不给抓的日期）。

    格式：{date: 2026-09-11, name: CPI, detail: "8月通胀", time: "08:30"}
    日期写错只会影响这一条，不会拖垮整个日历。
    """
    out: list[MacroEvent] = []
    for raw in entries or []:
        try:
            day = raw["date"]
            if isinstance(day, str):
                day = date.fromisoformat(day)
            at = None
            if raw.get("time"):
                hh, _, mm = str(raw["time"]).partition(":")
                at = time(int(hh), int(mm or 0))
            out.append(MacroEvent(day=day, name=str(raw["name"]),
                                  detail=str(raw.get("detail", "")),
                                  confirmed=True, at=at))
        except Exception as exc:
            log.warning("宏观日历配置项无法解析，已跳过：%s（%s）", raw, exc)
    return out


def next_fomc(today: date | None = None) -> MacroEvent | None:
    """下一次 FOMC 会议。

    单独取出来是因为它通常在 7 天窗口之外，但又是日程表里最重要的
    一项 —— 只显示窗口内的事件会让它长期不可见。
    """
    today = today or datetime.now(ET).date()
    future = [e for e in fomc_meetings() if e.day >= today]
    return future[0] if future else None


def upcoming(days: int = 7, today: date | None = None,
             extra: list[dict] | None = None) -> list[MacroEvent]:
    """未来若干天内的宏观事件，按日期排序。"""
    today = today or datetime.now(ET).date()
    end = today + timedelta(days=days)
    events = [e for e in fomc_meetings() if today <= e.day <= end]
    events += recurring_events(today, end)
    events += [e for e in parse_extra(extra) if today <= e.day <= end]
    return sorted(events, key=lambda e: (e.day, e.at or time(0, 0)))


# --------------------------------------------------------------------------
# 已发布数据的分级
# --------------------------------------------------------------------------

#  先排除同名的次要系列。Benzinga 的 "Redbook Retail Sales Index" 是
#  周度零售连锁店销售，与月度的 Retail Sales 完全不同；"ADP ... Weekly"
#  是周度就业估算，也远不及月度 ADP 报告重要。
#  （曾用负向前瞻 `Retail Sales(?!.*Redbook)` —— 无效，
#   因为 Redbook 出现在 Retail Sales 之前。）
EXCLUDE_PATTERNS = [
    re.compile(r"Redbook", re.I),
    re.compile(r"ADP.*Weekly|Weekly.*ADP", re.I),
    re.compile(r"House Price Index|Building Permits", re.I),
]

#  这些是真正会推动市场的发布，出现在新闻流里时必须置顶，
#  不能和房价指数、Redbook 零售之类的常规数据混在一起。
CORE_PATTERNS = [
    (re.compile(r"\bCPI\b|Consumer Price Index|通胀", re.I), "通胀"),
    (re.compile(r"\bPPI\b|Producer Price Index", re.I), "通胀"),
    (re.compile(r"\bPCE\b|Personal Consumption Expenditures", re.I), "通胀"),
    (re.compile(r"Nonfarm|Non-Farm|Employment Situation|Payrolls", re.I), "就业"),
    (re.compile(r"Unemployment Rate", re.I), "就业"),
    (re.compile(r"Initial Jobless Claims|Continuing Claims", re.I), "就业"),
    #  周度 ADP 已在 EXCLUDE 里滤掉，这里匹配的是月度全国就业报告
    (re.compile(r"ADP.*Employment", re.I), "就业"),
    (re.compile(r"\bFOMC\b|Fed Interest Rate|Federal Funds|Powell", re.I), "联储"),
    (re.compile(r"\bGDP\b|Gross Domestic Product", re.I), "增长"),
    (re.compile(r"Retail Sales", re.I), "消费"),
    (re.compile(r"\bISM\b|Manufacturing PMI|Services PMI", re.I), "景气"),
    (re.compile(r"Consumer Confidence|Consumer Sentiment", re.I), "信心"),
]


def classify(headline: str) -> str | None:
    """核心宏观发布返回其类别，否则返回 None。"""
    if any(p.search(headline) for p in EXCLUDE_PATTERNS):
        return None
    for pattern, label in CORE_PATTERNS:
        if pattern.search(headline):
            return label
    return None
