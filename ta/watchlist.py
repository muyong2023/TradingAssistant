"""watchlist 的增删改查。

直接对 config/config.yaml 做定点文本编辑，而不是 yaml.safe_load 后
整份重写 —— PyYAML 不保留注释，重写一次会把配置里所有说明性注释
全部抹掉（阈值为什么这么设、哪些是可调项），那些注释是配置的一半价值。

只改动 `symbols: [...]` 这一段，其余字节原样保留。
"""
from __future__ import annotations

import re
from pathlib import Path

from ta.config import CONFIG_PATH, config, watchlists


class WatchlistError(ValueError):
    pass


def _read(path: Path | None = None) -> tuple[Path, str]:
    p = path or CONFIG_PATH
    return p, p.read_text()


def groups(path: Path | None = None) -> dict[str, list[str]]:
    """当前各组的标的。"""
    if path:
        import yaml
        data = yaml.safe_load(path.read_text())
        return {k: list(v["symbols"]) for k, v in data["watchlists"].items()}
    return {k: list(v["symbols"]) for k, v in watchlists().items()}


def find_group(symbol: str, path: Path | None = None) -> str | None:
    symbol = symbol.upper()
    for name, symbols in groups(path).items():
        if symbol in symbols:
            return name
    return None


def _locate(text: str, group: str) -> tuple[int, int, list[str]]:
    """定位某组的 symbols 列表，返回（起, 止, 现有标的）。

    起止是方括号内容的字节区间，替换它即可，不触碰任何其他字符。
    """
    #  组名必须出现在 watchlists: 之下，且是二级缩进
    block = re.search(rf"^  {re.escape(group)}:\s*$", text, re.M)
    if not block:
        raise WatchlistError(f"配置里没有 {group} 这个组")
    #  从组名往下找第一个 symbols:，但不能越过下一个同级组
    rest = text[block.end():]
    next_group = re.search(r"^  \w+:\s*$", rest, re.M)
    scope = rest[: next_group.start()] if next_group else rest
    sym = re.search(r"symbols:\s*\[", scope)
    if not sym:
        raise WatchlistError(f"{group} 组里找不到 symbols 列表")

    open_at = block.end() + sym.end()          # 左括号之后
    close_at = text.find("]", open_at)
    if close_at < 0:
        raise WatchlistError(f"{group} 组的 symbols 列表没有闭合")
    current = [s.strip() for s in text[open_at:close_at].split(",") if s.strip()]
    return open_at, close_at, current


def _write(text: str, group: str, symbols: list[str], path: Path) -> None:
    open_at, close_at, _ = _locate(text, group)
    path.write_text(text[:open_at] + ", ".join(symbols) + text[close_at:])


def add(symbol: str, group: str, path: Path | None = None) -> str:
    """把标的加进某组。返回给用户看的说明。"""
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", symbol):
        raise WatchlistError(f"{symbol} 不像是有效的股票代码")

    p, text = _read(path)
    existing = find_group(symbol, path)
    if existing == group:
        return f"{symbol} 已经在 {group} 组里了"
    if existing:
        raise WatchlistError(f"{symbol} 已在 {existing} 组，先移除再加：/remove {symbol}")

    _, _, current = _locate(text, group)
    _write(text, group, current + [symbol], p)
    config.cache_clear()
    return f"已把 {symbol} 加入 {group}（该组现有 {len(current) + 1} 只）"


def remove(symbol: str, path: Path | None = None) -> str:
    symbol = symbol.strip().upper()
    p, text = _read(path)
    group = find_group(symbol, path)
    if not group:
        raise WatchlistError(f"{symbol} 不在任何组里")

    _, _, current = _locate(text, group)
    kept = [s for s in current if s != symbol]
    _write(text, group, kept, p)
    config.cache_clear()
    return f"已从 {group} 移除 {symbol}（该组还剩 {len(kept)} 只）"


def summary(path: Path | None = None) -> str:
    """给 Telegram 看的分组清单。"""
    data = config() if not path else None
    lines = []
    total = 0
    for name, symbols in groups(path).items():
        label = (data["watchlists"][name]["label"] if data else name)
        total += len(symbols)
        lines.append(f"<b>{label}</b>（{name}，{len(symbols)} 只）")
        lines.append("<code>" + " ".join(symbols) + "</code>" if symbols else "<i>（空）</i>")
    lines.append(f"\n共 {total} 只")
    return "\n".join(lines)
