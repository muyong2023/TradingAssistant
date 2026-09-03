"""测试共用配置。

测试此前直接读 config/config.yaml，于是生产配置一改开关（比如把
问答或涨跌幅告警关掉省额度），一大片测试就跟着挂——它们验证的是
"功能开着时行为对不对"，不该被运行时的开关左右。

这里给每个测试复制一份配置、把所有功能打开，并把 CONFIG_PATH 指过去。
各模块都是 `from ta.config import config` 绑定同一个函数对象，
所以改 CONFIG_PATH + 清缓存对全部调用方一致生效。
"""
from __future__ import annotations

import re

import pytest

from ta import config as C


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    #  用正则逐个改开关，而不是 yaml 读出再 dump 回去。
    #  safe_dump 会把 `symbols: [NVDA, MSFT]` 重排成块状列表，
    #  而 ta/watchlist.py 的定点编辑器只认方括号形式——曾因此让
    #  网页端的增删测试"假通过"（错误重定向与成功同为 303）。
    text = C.CONFIG_PATH.read_text()
    for key in ("enabled", "pct_move_alert", "rsi_alert",
                "premarket", "postclose", "intraday"):
        text = re.sub(rf"^(\s+{key}:\s*)false\b", r"\1true", text, flags=re.M | re.I)

    path = tmp_path / "config.yaml"
    path.write_text(text)
    monkeypatch.setattr(C, "CONFIG_PATH", path)
    monkeypatch.setattr("ta.watchlist.CONFIG_PATH", path)
    C.config.cache_clear()
    yield path
    C.config.cache_clear()
