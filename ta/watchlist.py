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
        if re.search(r"symbols:\s*$", scope, re.M):
            raise WatchlistError(
                f"{group} 组的 symbols 写成了多行列表，本工具只支持方括号写法。"
                f"请改成 symbols: [AAA, BBB] 后重试")
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


def validate(symbol: str) -> tuple[bool, str]:
    """校验代码是否真实存在，返回（是否有效, 说明）。

    行情接口对不存在的代码只是静默返回空数据，不报错 ——
    APPL（苹果应为 AAPL）当初就是这样混进配置的。
    网络不通时放行：宁可让人加进一个可疑代码，也不要因为断网卡住操作。
    """
    symbol = symbol.strip().upper()
    try:
        from ta.data.alpaca import AlpacaProvider
        asset = AlpacaProvider().get_asset(symbol)
    except Exception:
        return True, "（未能校验，已放行）"
    if asset is None:
        return False, f"{symbol} 在交易所资产库里查不到，请检查代码"
    name = asset.get("name", "")
    if not asset.get("tradable", True):
        return True, f"{name}（注意：当前不可交易）"
    return True, name


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


# --------------------------------------------------------------------------
# 分组的新建与删除
# --------------------------------------------------------------------------

GROUP_KEY = re.compile(r"[a-z][a-z0-9_]{1,23}")
DEFAULT_PCT = (10, 20)


def _watchlists_span(text: str) -> tuple[int, int]:
    """watchlists 块的字节区间（不含其后的顶级键）。"""
    head = re.search(r"^watchlists:\s*$", text, re.M)
    if not head:
        raise WatchlistError("配置里找不到 watchlists 段")
    rest = text[head.end():]
    nxt = re.search(r"^\S", rest, re.M)
    return head.end(), head.end() + (nxt.start() if nxt else len(rest))


def create_group(key: str, label: str, pct: tuple[float, float] = DEFAULT_PCT,
                 path: Path | None = None) -> str:
    """新建一个分组。"""
    key = key.strip().lower()
    label = label.strip() or key
    if not GROUP_KEY.fullmatch(key):
        raise WatchlistError(
            "分组标识只能用小写字母、数字和下划线，以字母开头，2–24 个字符")
    p, text = _read(path)
    if key in groups(path):
        raise WatchlistError(f"分组 {key} 已经存在")
    lo, hi = sorted(float(x) for x in pct)
    if lo <= 0 or hi <= 0:
        raise WatchlistError("告警阈值必须大于 0")

    block = (f"\n  {key}:\n"
             f'    label: "{label}"\n'
             f"    alert: {{ pct: [{lo:g}, {hi:g}] }}\n"
             f"    symbols: []\n")
    _, end = _watchlists_span(text)
    #  插在 watchlists 块的末尾。区间末尾通常是空行，插在其前面
    #  才不会把新组挤到下一个顶级键之后。
    insert_at = end
    while insert_at > 0 and text[insert_at - 1] == "\n":
        insert_at -= 1
    p.write_text(text[:insert_at] + "\n" + block.rstrip("\n") + "\n" + text[insert_at:])
    config.cache_clear()
    return f"已新建分组 {label}（{key}），告警阈值 ±{lo:g}% / ±{hi:g}%"


def delete_group(key: str, force: bool = False, path: Path | None = None) -> str:
    """删除一个分组。默认拒绝删非空的组，避免误删掉一串自选股。"""
    key = key.strip().lower()
    current = groups(path)
    if key not in current:
        raise WatchlistError(f"没有 {key} 这个分组")
    if len(current) <= 1:
        raise WatchlistError("至少要保留一个分组")
    if current[key] and not force:
        raise WatchlistError(
            f"{key} 里还有 {len(current[key])} 只标的（{', '.join(current[key])}），"
            f"先移除或确认强制删除")

    p, text = _read(path)
    start = re.search(rf"^  {re.escape(key)}:\s*$", text, re.M)
    if not start:
        raise WatchlistError(f"配置里定位不到 {key}")
    rest = text[start.end():]
    #  删到下一个同级组或下一个顶级键为止 —— 组内的注释随组一起删掉
    nxt = re.search(r"^(?:  \w+:\s*$|\S)", rest, re.M)
    end = start.end() + (nxt.start() if nxt else len(rest))
    p.write_text(text[:start.start()] + text[end:])
    config.cache_clear()
    return f"已删除分组 {key}" + (f"（连同 {len(current[key])} 只标的）"
                                 if current[key] else "")


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
