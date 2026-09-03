"""watchlist 增删测试。重点是：编辑后配置里的注释必须一字不少。"""
import pytest

from ta import watchlist as W
from ta.watchlist import WatchlistError

SAMPLE = """# 顶部说明
watchlists:
  # 这条注释必须活下来
  core_mega:
    label: "核心大盘"
    alert: { pct: [7, 12] }
    symbols: [NVDA, MSFT, AAPL]

  defensive:
    label: "消费防御"
    # 组内注释也要保留
    alert: { pct: [5, 9] }
    symbols: [KO, PG]

benchmarks: [SPY]
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE)
    return p


def test_add_appends_to_group(cfg):
    W.add("TSLA", "core_mega", path=cfg)
    assert W.groups(cfg)["core_mega"] == ["NVDA", "MSFT", "AAPL", "TSLA"]


def test_add_lowercase_is_normalized(cfg):
    W.add("tsla", "core_mega", path=cfg)
    assert "TSLA" in W.groups(cfg)["core_mega"]


def test_add_preserves_all_comments(cfg):
    """PyYAML 整份重写会抹掉全部注释，那是配置的一半价值。"""
    W.add("TSLA", "core_mega", path=cfg)
    text = cfg.read_text()
    assert "# 顶部说明" in text
    assert "# 这条注释必须活下来" in text
    assert "# 组内注释也要保留" in text


def test_add_does_not_touch_other_groups(cfg):
    W.add("TSLA", "core_mega", path=cfg)
    assert W.groups(cfg)["defensive"] == ["KO", "PG"]
    assert "alert: { pct: [5, 9] }" in cfg.read_text()


def test_add_duplicate_in_same_group_is_noop(cfg):
    msg = W.add("NVDA", "core_mega", path=cfg)
    assert "已经在" in msg
    assert W.groups(cfg)["core_mega"].count("NVDA") == 1


def test_add_duplicate_in_other_group_rejected(cfg):
    with pytest.raises(WatchlistError, match="已在 core_mega"):
        W.add("NVDA", "defensive", path=cfg)


def test_add_unknown_group_rejected(cfg):
    with pytest.raises(WatchlistError, match="没有 nonexistent"):
        W.add("TSLA", "nonexistent", path=cfg)


@pytest.mark.parametrize("bad", ["", "123", "nvda!", "a b", "TOOLONGSYMBOL1"])
def test_invalid_symbol_rejected(cfg, bad):
    with pytest.raises(WatchlistError):
        W.add(bad, "core_mega", path=cfg)


def test_remove(cfg):
    W.remove("MSFT", path=cfg)
    assert W.groups(cfg)["core_mega"] == ["NVDA", "AAPL"]


def test_remove_preserves_comments(cfg):
    W.remove("MSFT", path=cfg)
    assert "# 这条注释必须活下来" in cfg.read_text()


def test_remove_missing_symbol_rejected(cfg):
    with pytest.raises(WatchlistError, match="不在任何组"):
        W.remove("ZZZZ", path=cfg)


def test_remove_last_symbol_leaves_empty_list(cfg):
    W.remove("KO", path=cfg)
    W.remove("PG", path=cfg)
    assert W.groups(cfg)["defensive"] == []
    assert "symbols: []" in cfg.read_text()


def test_add_after_emptying_works(cfg):
    W.remove("KO", path=cfg)
    W.remove("PG", path=cfg)
    W.add("WMT", "defensive", path=cfg)
    assert W.groups(cfg)["defensive"] == ["WMT"]


def test_yaml_stays_parseable(cfg):
    import yaml
    W.add("TSLA", "core_mega", path=cfg)
    W.remove("KO", path=cfg)
    data = yaml.safe_load(cfg.read_text())
    assert data["watchlists"]["core_mega"]["symbols"] == ["NVDA", "MSFT", "AAPL", "TSLA"]
    assert data["benchmarks"] == ["SPY"]


def test_find_group(cfg):
    assert W.find_group("KO", path=cfg) == "defensive"
    assert W.find_group("ko", path=cfg) == "defensive"
    assert W.find_group("ZZZZ", path=cfg) is None
