"""测试共用配置。

测试此前直接读 config/config.yaml，于是生产配置一改开关（比如把
问答或涨跌幅告警关掉省额度），一大片测试就跟着挂——它们验证的是
"功能开着时行为对不对"，不该被运行时的开关左右。

这里给每个测试复制一份配置、把所有功能打开，并把 CONFIG_PATH 指过去。
各模块都是 `from ta.config import config` 绑定同一个函数对象，
所以改 CONFIG_PATH + 清缓存对全部调用方一致生效。
"""
from __future__ import annotations

import copy

import pytest
import yaml

from ta import config as C


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    data = copy.deepcopy(yaml.safe_load(C.CONFIG_PATH.read_text()))
    #  测试跑在"功能全开"的配置上，与生产开关解耦
    data.setdefault("chat", {})["enabled"] = True
    data.setdefault("alerts", {})["pct_move_alert"] = True
    data["alerts"]["rsi_alert"] = True
    data.setdefault("jobs", {}).update(
        {"premarket": True, "postclose": True, "intraday": True})
    data["indicators"].setdefault("intraday", {})["enabled"] = True

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    monkeypatch.setattr(C, "CONFIG_PATH", path)
    C.config.cache_clear()
    yield data
    C.config.cache_clear()
