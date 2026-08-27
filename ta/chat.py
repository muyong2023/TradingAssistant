"""对话问答：把助手已有的数据接给 Claude。

关键设计：不把 36 只票的全量数据一次塞进 prompt，而是做成工具让模型
按需调用。原因有二 —— 全量塞入每轮都要几万 token，且模型看到的永远是
快照；做成工具则问哪只查哪只，回答里的数字必定来自实时接口。

模型被明确要求：只依据工具返回的数据作答，不确定就说不知道，
不替用户做买卖决定。它的知识截止日期早于今天，任何"我记得 XX 公司…"
式的回忆都可能过时。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import anthropic
from anthropic import beta_tool

from ta import earnings as earnings_mod
from ta import macro
from ta.config import all_symbols, config, group_of, secrets, watchlists
from ta.data.news import AlpacaNews, compile_filters, rank
from ta.data.router import DataRouter
from ta.indicators import compute, rsi
from ta.market import ET, is_market_hours, last_session_close, now_et, session_fraction

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

_router = DataRouter()


def _snapshot(symbol: str):
    symbol = symbol.upper()
    quotes = _router.get_quotes([symbol])
    bars = _router.get_daily_bars([symbol], lookback_days=260)
    quote = quotes.get(symbol)
    series = bars.get(symbol)
    snap = (compute(symbol, series, config()["indicators"],
                    session_fraction=session_fraction()) if series else None)
    return quote, snap, series


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

@beta_tool
def get_watchlist() -> str:
    """列出用户关注的全部标的及其分组、告警阈值。

    当用户问"我关注了哪些票""我的持仓"或需要遍历全部标的时调用。
    """
    lines = []
    for name, group in watchlists().items():
        lo, hi = group["alert"]["pct"]
        lines.append(f"{group['label']}（{name}，告警 ±{lo}%/±{hi}%）："
                     f"{', '.join(group['symbols'])}")
    return "\n".join(lines)


@beta_tool
def get_quote(symbol: str) -> str:
    """查询单个标的的实时报价与当日涨跌。

    Args:
        symbol: 股票代码，如 NVDA。必须是美股代码。
    """
    quote, _, _ = _snapshot(symbol)
    if not quote:
        return f"{symbol}：取不到报价，可能代码有误或该标的不在数据源中。"
    return (f"{quote.symbol}  现价 ${quote.price:,.2f}  "
            f"当日 {quote.change_pct:+.2f}%  昨收 ${quote.prev_close:,.2f}  "
            f"数据源 {quote.source}  "
            f"{'盘中' if is_market_hours() else '休市'}")


@beta_tool
def get_indicators(symbol: str) -> str:
    """查询单个标的的技术指标：均线、RSI、量比、趋势判断。

    Args:
        symbol: 股票代码，如 NVDA。
    """
    quote, snap, _ = _snapshot(symbol)
    if not snap:
        return f"{symbol}：历史数据不足，无法计算指标。"
    parts = [f"{symbol}  收盘/现价 ${snap.close:,.2f}  趋势：{snap.trend()}"]
    if snap.rsi is not None:
        cfg = config()["indicators"]["rsi"]
        state = ("超卖" if snap.rsi <= cfg["oversold"]
                 else "超买" if snap.rsi >= cfg["overbought"] else "中性")
        parts.append(f"RSI(14) {snap.rsi:.1f}（{state}，阈值 "
                     f"{cfg['oversold']}/{cfg['overbought']}）")
    for period in (20, 50, 200):
        value, gap = snap.sma.get(period), snap.sma_gap_pct.get(period)
        if value is None:
            parts.append(f"MA{period}：历史不足 {period} 个交易日")
        else:
            parts.append(f"MA{period} ${value:,.2f}（现价偏离 {gap:+.1f}%）")
    if snap.volume_ratio:
        note = "，盘中按时段折算的预估值" if snap.volume_ratio_projected else ""
        parts.append(f"量比 {snap.volume_ratio:.2f}x{note}")
    parts.append(f"所属分组：{group_of(symbol)}")
    return "\n".join(parts)


@beta_tool
def get_price_history(symbol: str, days: int = 30) -> str:
    """查询近期日线收盘价序列，用于判断走势形态。

    Args:
        symbol: 股票代码。
        days: 回溯的交易日数量，最多 120。
    """
    _, _, series = _snapshot(symbol)
    if not series:
        return f"{symbol}：无历史数据。"
    window = series[-min(max(days, 2), 120):]
    first, last = window[0], window[-1]
    change = (last.close - first.close) / first.close * 100
    rows = [f"{b.day} 收 {b.close:,.2f} 量 {b.volume:,.0f}" for b in window[-12:]]
    return (f"{symbol} 近 {len(window)} 个交易日："
            f"{first.day} ${first.close:,.2f} → {last.day} ${last.close:,.2f}"
            f"（{change:+.1f}%）\n"
            f"区间最高 ${max(b.high for b in window):,.2f} "
            f"最低 ${min(b.low for b in window):,.2f}\n"
            f"最近 12 根：\n" + "\n".join(rows))


@beta_tool
def get_movers(limit: int = 8) -> str:
    """查询关注列表里当日涨跌幅最大的标的。

    当用户问"今天什么涨得好""哪只跌得多""大盘怎么样"时调用。

    Args:
        limit: 返回条数，默认 8。
    """
    symbols = all_symbols()
    quotes = _router.get_quotes(symbols)
    rows = sorted((q for q in quotes.values()),
                  key=lambda q: q.change_pct, reverse=True)
    n = max(1, min(limit, 15))
    top, bottom = rows[:n], rows[-n:][::-1]
    up = sum(1 for q in rows if q.change_pct > 0)
    out = [f"共 {len(rows)} 只，{up} 涨 {len(rows) - up} 跌"]
    out.append("涨幅前列：" + "　".join(
        f"{q.symbol} {q.change_pct:+.2f}%" for q in top))
    out.append("跌幅前列：" + "　".join(
        f"{q.symbol} {q.change_pct:+.2f}%" for q in bottom))
    return "\n".join(out)


@beta_tool
def get_news(symbol: str = "", hours: int = 24, limit: int = 8) -> str:
    """查询关注列表相关的近期新闻。

    Args:
        symbol: 指定标的则只返回它的新闻；留空返回全部关注标的的新闻。
        hours: 回溯小时数，默认 24。
        limit: 返回条数，默认 8。
    """
    symbols = [symbol.upper()] if symbol else all_symbols()
    since = now_et() - timedelta(hours=max(1, min(hours, 168)))
    try:
        items = AlpacaNews().fetch(symbols, since, limit=60)
    except Exception as exc:
        return f"新闻获取失败：{exc}"
    cfg = config().get("news", {})
    picked = rank(items, limit=max(1, min(limit, 15)),
                  per_symbol=3 if symbol else 2,
                  filters=compile_filters(cfg.get("exclude_patterns")))
    if not picked:
        return "该时间范围内没有相关新闻。"
    return "\n".join(
        f"[{'/'.join(n.symbols[:3])}] {n.created_at.astimezone(ET):%m-%d %H:%M} "
        f"{n.headline}" for n in picked)


@beta_tool
def get_macro_calendar(days: int = 14) -> str:
    """查询未来的宏观经济日程：FOMC 会议、CPI、非农就业等。

    Args:
        days: 前瞻天数，默认 14。
    """
    days = max(1, min(days, 60))
    try:
        events = macro.upcoming(days, extra=config().get("macro", {}).get("extra_events"))
        fomc = macro.next_fomc()
    except Exception as exc:
        return f"宏观日历获取失败：{exc}"
    lines = [f"{e.day}（{e.day.strftime('%a')}）"
             f"{e.at.strftime(' %H:%M ET') if e.at else ''}　{e.label()}"
             for e in events]
    if fomc:
        lines.append(f"下次 FOMC：{fomc.day}（{(fomc.day - date.today()).days} 天后）"
                     f"　{fomc.detail}")
    return "\n".join(lines) if lines else "该区间内无重要发布。"


@beta_tool
def get_earnings(symbol: str = "", days: int = 60) -> str:
    """查询财报日期与分析师的 EPS / 营收预期。

    Args:
        symbol: 指定标的则只返回它的；留空返回窗口内全部关注标的的。
        days: 前瞻天数，默认 60。
    """
    try:
        events = earnings_mod.all_events(all_symbols())
    except Exception as exc:
        return f"财报日程获取失败：{exc}"
    if symbol:
        hit = next((e for e in events if e.symbol == symbol.upper()), None)
        return hit.label() + f"　日期 {hit.day}" if hit else f"{symbol}：无财报日期数据（ETF 无财报）。"
    today = now_et().date()
    end = today + timedelta(days=max(1, min(days, 180)))
    window = sorted((e for e in events if today <= e.day <= end), key=lambda e: e.day)
    if not window:
        return f"未来 {days} 天内关注列表无财报。"
    return "\n".join(f"{e.day}　{e.label()}" for e in window)


TOOLS = [get_watchlist, get_quote, get_indicators, get_price_history,
         get_movers, get_news, get_macro_calendar, get_earnings]


# --------------------------------------------------------------------------
# 对话
# --------------------------------------------------------------------------

SYSTEM = """你是一个美股看盘助手，通过 Telegram 与用户对话。用户是这个助手的
拥有者，关注列表和持仓都在工具里。

**数据纪律**
- 任何具体数字（价格、涨跌幅、指标、日期、财报预期）都必须来自工具返回的结果。
- 你的训练数据有截止日期，早于今天。不要凭记忆陈述公司近况、股价水平、
  财报结果或宏观数据 —— 那些很可能已经过时。需要就调工具。
- 工具查不到的，直接说查不到，不要推测填补。
- 用户问某只票时，通常需要同时看报价和指标，一并调用。

**回答方式**
- 这是 Telegram，回答要短。默认三到五句话，除非用户要求展开。
- 用 Telegram HTML 语法：<b>粗体</b>、<i>斜体</i>、<code>等宽</code>。
  不要用 Markdown 的 ** 或 ##，不要用表格，Telegram 不渲染它们。
- 先给结论，再给支撑它的一两个数据。不要复述工具的全部输出。
- 中文回答。股票代码保持英文大写。

**立场**
- 你提供数据和分析，不替用户做买卖决定。可以说"RSI 已到 82，
  按你设的阈值属超买区"，不要说"现在该卖出"。
- 用户问"该不该买"时，给出支持和反对的依据、需要留意的风险点，
  以及他自己设定的规则怎么看待当前状态，把决定权留给他。
- 不需要每条消息都加免责声明，用户知道这是工具。"""


def build_system() -> list[dict]:
    """系统提示 + 随时间变化的上下文。

    把易变部分（当前时间、市场状态）放在缓存断点之后，
    保证前面的长指令能命中 prompt 缓存。
    """
    now = now_et()
    state = "盘中交易时段" if is_market_hours() else "休市"
    since = last_session_close()
    return [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text",
         "text": (f"当前时间：{now:%Y-%m-%d %H:%M} ET（{now:%A}），{state}。"
                  f"上一交易时段收盘：{since:%Y-%m-%d %H:%M} ET。")},
    ]


def ask(question: str, history: list[dict] | None = None) -> tuple[str, list[dict]]:
    """回答一个问题，返回（回复文本, 更新后的对话历史）。"""
    s = secrets()
    s.require("anthropic_api_key")
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)

    messages = list(history or []) + [{"role": "user", "content": question}]
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=build_system(),
        tools=TOOLS,
        messages=messages,
        thinking={"type": "adaptive"},
    )

    final = None
    for message in runner:
        final = message
        messages.append({"role": "assistant", "content": message.content})
        tool_response = runner.generate_tool_call_response()
        if tool_response is not None:
            messages.append(tool_response)

    if final is None:
        return "（没有得到回复，请重试）", list(history or [])
    if final.stop_reason == "refusal":
        return "这个问题我不便回答，换个问法试试。", list(history or [])

    text = "\n".join(b.text for b in final.content
                     if b.type == "text" and b.text.strip())
    return text or "（模型没有返回文本）", messages
