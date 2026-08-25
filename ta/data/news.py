"""新闻抓取（Alpaca / Benzinga）。

与行情共用同一套 Alpaca 凭据，无需额外申请。

选取逻辑的要点：一篇文章常同时挂十几个标的（"十只值得关注的 AI 股"
这类综述），这种泛泛之谈的信息量远低于只挂一两只票的个股新闻。
故排序以"标的专一度"为主，而非单纯按时间倒序 ——
否则晨报会被行业综述刷屏，真正的个股消息反而沉底。
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from ta.config import secrets
from ta.data.base import DataError

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
TIMEOUT = 20
MAX_PAGES = 4

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    id: int
    created_at: datetime
    headline: str
    summary: str
    source: str
    url: str
    #  与 watchlist 有交集的标的
    symbols: tuple[str, ...]
    #  文章原本挂的全部标的，用于判断专一度
    all_symbols: tuple[str, ...]

    @property
    def is_broad(self) -> bool:
        """挂了很多标的的综述类文章。"""
        return len(self.all_symbols) >= 5

    @property
    def has_article(self) -> bool:
        """是否有正文。

        Benzinga 会把宏观数据发布（房价指数、ADP 就业、营建许可等）
        也发成"新闻"，但链接指向的是行情页而非文章。这类条目全部
        挂在 SPY 名下，会挤占它有限的名额把真正的市场新闻顶掉，
        故与正文新闻分开处理。
        """
        return bool(self.url) and "/quote/" not in self.url

    def specificity(self) -> float:
        return 1.0 / max(len(self.all_symbols), 1)


class AlpacaNews:
    name = "alpaca-news"

    def __init__(self) -> None:
        s = secrets()
        s.require("alpaca_key_id", "alpaca_secret")
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": s.alpaca_key_id,
            "APCA-API-SECRET-KEY": s.alpaca_secret,
        })

    def fetch(self, symbols: list[str], since: datetime,
              limit: int = 50) -> list[NewsItem]:
        if not symbols:
            return []
        watch = {s.upper() for s in symbols}
        params = {
            "symbols": ",".join(symbols),
            "start": since.astimezone(timezone.utc).isoformat(),
            "limit": min(limit, 50),
            "sort": "desc",
            "include_content": "false",
        }
        items: list[NewsItem] = []
        seen: set[int] = set()
        token = None
        for _ in range(MAX_PAGES):
            if token:
                params["page_token"] = token
            try:
                resp = self._session.get(NEWS_URL, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise DataError(f"新闻请求失败: {exc}") from exc
            if resp.status_code != 200:
                raise DataError(f"新闻接口返回 {resp.status_code}: {resp.text[:200]}")
            payload = resp.json()
            for raw in payload.get("news", []):
                item = _parse(raw, watch)
                if item and item.id not in seen:
                    seen.add(item.id)
                    items.append(item)
            token = payload.get("next_page_token")
            if not token or len(items) >= limit:
                break
        return items


def _parse(raw: dict, watch: set[str]) -> NewsItem | None:
    all_syms = tuple(raw.get("symbols") or ())
    hit = tuple(s for s in all_syms if s in watch)
    if not hit:
        return None
    #  接口返回的标题里已含 HTML 实体（如 &amp;），先还原成字面文本，
    #  否则渲染时再转义一次会变成 &amp;amp; 显示出来
    headline = html.unescape((raw.get("headline") or "").strip())
    if not headline:
        return None
    try:
        created = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return NewsItem(
        id=int(raw.get("id", 0)),
        created_at=created,
        headline=headline,
        summary=html.unescape((raw.get("summary") or "").strip()),
        source=raw.get("source", ""),
        url=raw.get("url", ""),
        symbols=hit,
        all_symbols=all_syms,
    )


def compile_filters(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in (patterns or [])]


def is_noise(item: NewsItem, filters: list[re.Pattern]) -> bool:
    """匹配模板化的自动生成稿。

    Benzinga 会按模板批量生成"Competitor Analysis: …""Performance
    Comparison: …"这类稿件，标题工整但没有增量信息。规则放在配置里
    而非写死，因为模板会变，也因人而异。
    """
    return any(f.search(item.headline) for f in filters)


def data_releases(items: list[NewsItem], limit: int = 4) -> list[NewsItem]:
    """无正文的数据发布，按时间倒序，同一份数据只留一条。

    "USA Building Permits For July" 与 "USA Building Permits (MoM) For July"
    是同一次发布的两个口径，按标题前几个词去重。
    """
    out: list[NewsItem] = []
    seen: set[str] = set()
    for item in sorted(items, key=lambda n: n.created_at, reverse=True):
        if item.has_article:
            continue
        key = " ".join(item.headline.lower().split()[:3])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def rank(items: list[NewsItem], boosted: set[str] | None = None,
         limit: int = 10, per_symbol: int = 2,
         filters: list[re.Pattern] | None = None) -> list[NewsItem]:
    """挑出最值得读的几条。

    boosted 是"今天本来就该关注"的标的（有告警、RSI 极值、大幅波动），
    它们的新闻优先。其余按标的专一度排序，同分再按时间倒序。

    per_symbol 是每只票的条数上限，不设它的话热门票会独占整份晨报 ——
    实测 NVDA 一夜有几十条，会把 10 条名额占掉 9 条，
    其余 35 只票一条都挤不进来。
    """
    boosted = boosted or set()

    #  只排有正文的；数据发布交给 data_releases 单独处理

    def score(item: NewsItem) -> tuple:
        hits_boost = any(s in boosted for s in item.symbols)
        return (
            not hits_boost,              # 关注标的优先（False 排前）
            not item.specificity() >= 0.5,   # 只挂 1-2 只票的优先
            len(item.all_symbols),       # 挂得越少越靠前
            -item.created_at.timestamp(),
        )

    counts: dict[str, int] = {}
    picked: list[NewsItem] = []
    filters = filters or []
    pool = (n for n in items if n.has_article and not is_noise(n, filters))
    for item in sorted(pool, key=score):
        if len(picked) >= limit:
            break
        #  任一涉及标的到顶即跳过。先前用"最少的那个没到顶就收"，
        #  结果 NVDA 一路搭别的票的顺风车，10 条里仍占 8 条。
        #  多标的文章被已达上限的标的挡住是可接受的 ——
        #  那个主题本来就已经覆盖过了。
        if any(counts.get(s, 0) >= per_symbol for s in item.symbols):
            continue
        picked.append(item)
        for s in item.symbols:
            counts[s] = counts.get(s, 0) + 1
    return picked
