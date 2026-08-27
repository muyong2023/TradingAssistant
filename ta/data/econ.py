"""经济数据发布日历。

两个源，优先级不同：

- **FRED**（美联储圣路易斯分行官方 API）—— 覆盖完整、日期权威，
  但需要一把免费 key。配了就用它。
- **Nasdaq**（无需 key）—— 兜底。较远的未来日期填充不全
  （2026-09 的 CPI 查不到），故只作补充。

**Nasdaq 接口的 date 参数偏移一天**：查 date=D 返回的是 D-1 天的事件。
实测依据：2026-08-25 当天实际发布的房价指数 442.5、Redbook 9.1%、
营建许可 1.433M，全部出现在 date=2026-08-26 之下且已带实际值；
而 date=2026-08-24 返回空——8/23 是周日。修正后所有日期自洽：
初请失业金回到周四，联储决议回到 9/16，与美联储官网的
9/15-16 会议吻合。

FOMC 仍一律以美联储官网为准（ta/macro.py），这里滤掉联储条目，
避免同一事件出现两次。
"""
from __future__ import annotations

import json
import logging
import re
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import requests

from ta.config import ROOT, redact, secrets

log = logging.getLogger(__name__)

CACHE_PATH = ROOT / "data" / "econ_cache.json"
#  已过去的日期不会再变；未来的日期会随着源逐步填充，故只缓存数小时
CACHE_TTL_SECONDS = 6 * 3600

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/economicevents"
FRED_URL = "https://api.stlouisfed.org/fred"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 20

#  想在日历里看到的发布。键是展示名，值是匹配用的正则。
WANTED = [
    ("CPI", re.compile(r"^(Core )?CPI\b(?!.*Cleveland)|^CPI Index", re.I), "通胀"),
    ("PPI", re.compile(r"^(Core )?PPI\b", re.I), "通胀"),
    ("PCE 物价", re.compile(r"PCE Price[s]? [Ii]ndex|^Core PCE", re.I), "通胀"),
    ("非农就业", re.compile(r"Nonfarm Payrolls|Non-Farm Payrolls", re.I), "就业"),
    ("失业率", re.compile(r"^Unemployment Rate", re.I), "就业"),
    ("初请失业金", re.compile(r"^Initial Jobless Claims", re.I), "就业"),
    ("零售销售", re.compile(r"^(Core )?Retail Sales", re.I), "消费"),
    ("GDP", re.compile(r"^GDP\b", re.I), "增长"),
    ("ISM 制造业", re.compile(r"^ISM Manufacturing PMI", re.I), "景气"),
    ("ISM 非制造业", re.compile(r"^ISM Non-Manufacturing PMI", re.I), "景气"),
    ("消费者信心", re.compile(r"^(CB )?Consumer Confidence|Michigan Consumer Sentiment", re.I), "信心"),
]

#  联储相关一律走美联储官网，避免与 Nasdaq 的错误日期冲突
_FED = re.compile(r"Fed Interest Rate|FOMC|Fed Funds", re.I)


@dataclass(frozen=True)
class EconEvent:
    day: date
    name: str
    category: str
    at: time | None = None
    source: str = "nasdaq"


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _cache_get(key: str) -> list[EconEvent] | None:
    entry = _cache_load().get(key)
    if not entry:
        return None
    if time_module.time() - entry.get("ts", 0) > CACHE_TTL_SECONDS:
        return None
    return [EconEvent(day=date.fromisoformat(e["day"]), name=e["name"],
                      category=e["category"],
                      at=time.fromisoformat(e["at"]) if e.get("at") else None,
                      source=e.get("source", "")) for e in entry["events"]]


def _cache_put(key: str, events: list[EconEvent]) -> None:
    data = _cache_load()
    #  只保留最近的若干条目，避免文件无限增长
    data[key] = {
        "ts": time_module.time(),
        "events": [{"day": e.day.isoformat(), "name": e.name,
                    "category": e.category,
                    "at": e.at.isoformat() if e.at else None,
                    "source": e.source} for e in events],
    }
    if len(data) > 40:
        for k in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - 40]:
            del data[k]
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        log.debug("经济日历缓存写入失败：%s", exc)


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _match(name: str) -> tuple[str, str] | None:
    for label, pattern, category in WANTED:
        if pattern.search(name):
            return label, category
    return None


def fetch_nasdaq(day: date, use_cache: bool = True) -> list[EconEvent]:
    """取某一天的美国经济事件。

    接口的 date 参数偏移一天（见模块说明），故查询时加一天，
    返回的事件按 day 标注。
    """
    key = f"nasdaq:{day.isoformat()}"
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    query_day = day + timedelta(days=1)
    try:
        resp = requests.get(NASDAQ_URL, params={"date": query_day.isoformat()},
                            headers={"User-Agent": UA, "Accept": "application/json"},
                            timeout=TIMEOUT)
        resp.raise_for_status()
        rows = (resp.json().get("data") or {}).get("rows") or []
    except Exception as exc:
        log.debug("Nasdaq 日历 %s 获取失败：%s", day, redact(exc))
        return []

    seen: set[str] = set()
    out: list[EconEvent] = []
    for row in rows:
        if row.get("country") != "United States":
            continue
        name = _clean(row.get("eventName", ""))
        if _FED.search(name):
            continue
        hit = _match(name)
        if not hit:
            continue
        label, category = hit
        if label in seen:      # 同一发布常拆成好几个口径，只留一条
            continue
        seen.add(label)
        out.append(EconEvent(day=day, name=label, category=category,
                             at=_parse_time(row.get("gmt")), source="nasdaq"))
    if use_cache:
        _cache_put(key, out)
    return out


def _parse_time(text) -> time | None:
    if not text:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})", str(text))
    if not m:
        return None
    try:
        return time(int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


#  FRED 的 release id。**必须用 /fred/releases 核对过再写**——
#  凭印象填的 id 会静默地把张冠李戴的数据展示出来：
#  54 不是"就业报告"而是 Personal Income and Outlays（PCE），
#  21 不是"零售销售"而是 H.6 Money Stock Measures。
#  ISM 与谘商会消费者信心是私营机构发布，FRED 没有，仍走 Nasdaq。
FRED_RELEASES = {
    10: ("CPI", "通胀"),
    46: ("PPI", "通胀"),
    54: ("PCE 物价", "通胀"),
    50: ("非农就业", "就业"),
    180: ("初请失业金", "就业"),
    9: ("零售销售", "消费"),
    53: ("GDP", "增长"),
}


def fetch_fred(start: date, end: date, use_cache: bool = True) -> list[EconEvent]:
    """用 FRED 官方 API 取发布日期。没有 key 时返回空。"""
    key = secrets().fred_api_key
    if not key:
        return []
    cache_key = f"fred:{start.isoformat()}:{end.isoformat()}"
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            return hit
    out: list[EconEvent] = []
    for release_id, (label, category) in FRED_RELEASES.items():
        try:
            resp = requests.get(
                f"{FRED_URL}/release/dates",
                params={"release_id": release_id, "api_key": key,
                        "file_type": "json", "include_release_dates_with_no_data": "true",
                        "realtime_start": start.isoformat(),
                        "realtime_end": end.isoformat()},
                timeout=TIMEOUT)
            resp.raise_for_status()
            for row in resp.json().get("release_dates", []):
                day = date.fromisoformat(row["date"])
                if start <= day <= end:
                    out.append(EconEvent(day=day, name=label, category=category,
                                         at=time(8, 30), source="fred"))
        except Exception as exc:
            #  必须脱敏：api_key 在查询串里，异常消息会带上整条 URL
            log.warning("FRED release %s 获取失败：%s", release_id, redact(exc))
    if use_cache and out:
        _cache_put(cache_key, out)
    return out


#  同一份报告里的次要指标：主指标在场时不必单独列出。
#  失业率与非农就业同出于 BLS 的 Employment Situation。
SUBSUMED_BY = {"失业率": "非农就业"}


def upcoming(start: date, days: int = 7) -> list[EconEvent]:
    """区间内的经济发布。优先 FRED，缺失的日期用 Nasdaq 补。"""
    end = start + timedelta(days=days)
    events = fetch_fred(start, end)
    have = {(e.day, e.name) for e in events}

    day = start
    while day <= end:
        if day.weekday() < 5:
            for e in fetch_nasdaq(day):
                if (e.day, e.name) not in have:
                    events.append(e)
                    have.add((e.day, e.name))
        day += timedelta(days=1)

    names_by_day: dict[date, set[str]] = {}
    for e in events:
        names_by_day.setdefault(e.day, set()).add(e.name)
    events = [e for e in events
              if SUBSUMED_BY.get(e.name) not in names_by_day.get(e.day, set())]

    return sorted(events, key=lambda e: (e.day, e.at or time(0, 0), e.name))
