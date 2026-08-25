"""新闻抓取与排序测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from ta.data.news import NewsItem, _parse, rank


def item(nid, symbols, all_symbols=None, minutes_ago=0):
    all_symbols = tuple(all_symbols or symbols)
    return NewsItem(
        id=nid,
        created_at=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago),
        headline=f"headline-{nid}", summary="", source="benzinga",
        url=f"https://example.com/{nid}",
        symbols=tuple(symbols), all_symbols=all_symbols,
    )


def test_parse_filters_to_watchlist():
    raw = {"id": 1, "created_at": "2026-08-25T13:00:00Z", "headline": "x",
           "symbols": ["NVDA", "SOME", "OTHER"], "source": "benzinga", "url": "u"}
    parsed = _parse(raw, {"NVDA", "KO"})
    assert parsed.symbols == ("NVDA",)
    assert parsed.all_symbols == ("NVDA", "SOME", "OTHER")


def test_parse_skips_when_no_watchlist_overlap():
    raw = {"id": 1, "created_at": "2026-08-25T13:00:00Z", "headline": "x",
           "symbols": ["ZZZZ"], "source": "b", "url": "u"}
    assert _parse(raw, {"NVDA"}) is None


def test_parse_skips_empty_headline():
    raw = {"id": 1, "created_at": "2026-08-25T13:00:00Z", "headline": "  ",
           "symbols": ["NVDA"], "source": "b", "url": "u"}
    assert _parse(raw, {"NVDA"}) is None


def test_parse_skips_bad_timestamp():
    raw = {"id": 1, "created_at": "not-a-date", "headline": "x",
           "symbols": ["NVDA"], "source": "b", "url": "u"}
    assert _parse(raw, {"NVDA"}) is None


def test_specific_articles_outrank_roundups():
    items = [item(1, ["NVDA"], ["NVDA"] + [f"X{i}" for i in range(9)]),
             item(2, ["KO"], ["KO"])]
    assert [n.id for n in rank(items, limit=2)] == [2, 1]


def test_boosted_symbols_come_first():
    items = [item(1, ["KO"]), item(2, ["NVDA"])]
    assert rank(items, boosted={"NVDA"}, limit=2)[0].id == 2


def test_per_symbol_cap_prevents_domination():
    """曾经的 bug：NVDA 一夜几十条，10 个名额占掉 8 个。"""
    hot = [item(i, ["NVDA"]) for i in range(20)]
    others = [item(100 + i, [s]) for i, s in enumerate(["KO", "JNJ", "PG", "MCD"])]
    picked = rank(hot + others, limit=6, per_symbol=2)
    counts = {}
    for n in picked:
        for s in n.symbols:
            counts[s] = counts.get(s, 0) + 1
    assert counts["NVDA"] == 2
    assert len(counts) >= 4


def test_hot_symbol_cannot_ride_along_on_multi_symbol_articles():
    """严格上限：NVDA 到顶后，即便文章还挂着别的票也不再收。"""
    items = ([item(i, ["NVDA"]) for i in range(3)]
             + [item(50 + i, ["NVDA", "KO"]) for i in range(3)])
    picked = rank(items, limit=6, per_symbol=2)
    nvda = sum(1 for n in picked for s in n.symbols if s == "NVDA")
    assert nvda == 2


def test_respects_limit():
    assert len(rank([item(i, ["S%d" % i]) for i in range(30)], limit=5)) == 5


def test_empty_input():
    assert rank([], limit=10) == []


def test_is_broad_threshold():
    assert item(1, ["NVDA"], ["A", "B", "C", "D", "E"]).is_broad is True
    assert item(2, ["NVDA"], ["A", "B"]).is_broad is False


def quote_item(nid, headline, symbols=("SPY",)):
    return NewsItem(
        id=nid, created_at=datetime(2026, 8, 25, 12, nid % 60, tzinfo=timezone.utc),
        headline=headline, summary="", source="benzinga",
        url="https://www.benzinga.com/quote/SPY",
        symbols=tuple(symbols), all_symbols=tuple(symbols),
    )


def test_has_article_detects_quote_pages():
    assert item(1, ["NVDA"]).has_article is True
    assert quote_item(1, "USA Building Permits For July").has_article is False
    assert NewsItem(1, datetime.now(timezone.utc), "h", "", "s", "",
                    ("NVDA",), ("NVDA",)).has_article is False


def test_rank_excludes_data_releases():
    """宏观数据帖全挂在 SPY 名下，会挤占它的名额把真新闻顶掉。"""
    from ta.data.news import data_releases
    items = [quote_item(i, f"Data Release {i}") for i in range(8)] + [item(99, ["SPY"])]
    picked = rank(items, limit=10)
    assert [n.id for n in picked] == [99]
    assert len(data_releases(items)) > 0


def test_data_releases_dedupes_same_release():
    """同一次发布的两个口径（YoY / MoM）只留一条。"""
    from ta.data.news import data_releases
    items = [quote_item(1, "USA Building Permits For July 1.433M"),
             quote_item(2, "USA Building Permits (MoM) For July 4.3%"),
             quote_item(3, "ADP Employment Change Weekly 11.75K")]
    out = data_releases(items)
    assert len(out) == 2


def test_data_releases_respects_limit():
    from ta.data.news import data_releases
    items = [quote_item(i, f"Unique Release {i} value") for i in range(10)]
    assert len(data_releases(items, limit=3)) == 3


def test_data_releases_ignores_real_articles():
    from ta.data.news import data_releases
    assert data_releases([item(1, ["NVDA"]), item(2, ["KO"])]) == []


def test_headline_entities_unescaped_once():
    """接口返回的标题已含 &amp;，渲染时再转义会显示成 &amp;amp;。"""
    raw = {"id": 1, "created_at": "2026-08-25T13:00:00Z",
           "headline": "Storage &amp; Peripherals Industry",
           "symbols": ["AAPL"], "source": "b", "url": "https://x/a"}
    assert _parse(raw, {"AAPL"}).headline == "Storage & Peripherals Industry"


def test_noise_filter_drops_template_articles():
    from ta.data.news import compile_filters
    filters = compile_filters(["^Competitor Analysis:", "^In-Depth Analysis:"])
    keep = item(1, ["NVDA"])
    drop = NewsItem(2, keep.created_at, "Competitor Analysis: Evaluating Apple",
                    "", "b", "https://x/b", ("AAPL",), ("AAPL",))
    picked = rank([keep, drop], limit=5, filters=filters)
    assert [n.id for n in picked] == [1]


def test_no_filters_keeps_everything():
    items = [item(1, ["NVDA"]), item(2, ["KO"])]
    assert len(rank(items, limit=5, filters=[])) == 2
