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


# --------------------------------------------------------------------------
# 分组管理
# --------------------------------------------------------------------------

def test_create_group(cfg):
    W.create_group("dividend", "高股息", (6, 11), path=cfg)
    assert "dividend" in W.groups(cfg)
    assert W.groups(cfg)["dividend"] == []


def test_created_group_has_correct_settings(cfg):
    import yaml
    W.create_group("dividend", "高股息", (6, 11), path=cfg)
    data = yaml.safe_load(cfg.read_text())["watchlists"]["dividend"]
    assert data["label"] == "高股息"
    assert data["alert"]["pct"] == [6, 11]
    assert data["symbols"] == []


def test_create_then_add_symbol(cfg):
    W.create_group("dividend", "高股息", path=cfg)
    W.add("KMB", "dividend", path=cfg)
    assert W.groups(cfg)["dividend"] == ["KMB"]


def test_create_preserves_existing_groups_and_comments(cfg):
    W.create_group("dividend", "高股息", path=cfg)
    assert W.groups(cfg)["core_mega"] == ["NVDA", "MSFT", "AAPL"]
    text = cfg.read_text()
    assert "# 这条注释必须活下来" in text
    assert "# 组内注释也要保留" in text


def test_create_does_not_break_following_top_level_keys(cfg):
    """新组必须插在 watchlists 块内，不能挤到 benchmarks 之后。"""
    import yaml
    W.create_group("dividend", "高股息", path=cfg)
    data = yaml.safe_load(cfg.read_text())
    assert data["benchmarks"] == ["SPY"]
    assert "dividend" in data["watchlists"]


def test_create_duplicate_rejected(cfg):
    with pytest.raises(WatchlistError, match="已经存在"):
        W.create_group("core_mega", "重复", path=cfg)


@pytest.mark.parametrize("bad", ["", "1abc", "Has-Upper", "with space", "a", "x" * 30])
def test_invalid_group_key_rejected(cfg, bad):
    with pytest.raises(WatchlistError):
        W.create_group(bad, "标签", path=cfg)


def test_thresholds_sorted_regardless_of_input_order(cfg):
    import yaml
    W.create_group("dividend", "高股息", (20, 5), path=cfg)
    assert yaml.safe_load(cfg.read_text())["watchlists"]["dividend"]["alert"]["pct"] == [5, 20]


def test_nonpositive_threshold_rejected(cfg):
    with pytest.raises(WatchlistError, match="大于 0"):
        W.create_group("dividend", "高股息", (0, 10), path=cfg)


def test_delete_empty_group(cfg):
    W.create_group("dividend", "高股息", path=cfg)
    W.delete_group("dividend", path=cfg)
    assert "dividend" not in W.groups(cfg)


def test_delete_nonempty_group_requires_force(cfg):
    """一条命令删掉一串自选股太容易误操作。"""
    with pytest.raises(WatchlistError, match="还有 2 只"):
        W.delete_group("defensive", path=cfg)
    assert "defensive" in W.groups(cfg)


def test_force_delete_nonempty_group(cfg):
    W.delete_group("defensive", force=True, path=cfg)
    assert "defensive" not in W.groups(cfg)


def test_delete_last_group_rejected(cfg):
    W.delete_group("defensive", force=True, path=cfg)
    with pytest.raises(WatchlistError, match="至少要保留一个"):
        W.delete_group("core_mega", force=True, path=cfg)


def test_delete_unknown_group_rejected(cfg):
    with pytest.raises(WatchlistError, match="没有 nope"):
        W.delete_group("nope", path=cfg)


def test_delete_keeps_other_groups_intact(cfg):
    W.delete_group("defensive", force=True, path=cfg)
    assert W.groups(cfg)["core_mega"] == ["NVDA", "MSFT", "AAPL"]
    assert "# 这条注释必须活下来" in cfg.read_text()


def test_yaml_valid_after_create_and_delete_cycles(cfg):
    import yaml
    for i in range(3):
        W.create_group(f"grp{i}", f"组{i}", path=cfg)
        W.add("AAA", f"grp{i}", path=cfg)
        W.remove("AAA", path=cfg)
        W.delete_group(f"grp{i}", path=cfg)
    data = yaml.safe_load(cfg.read_text())
    assert list(data["watchlists"]) == ["core_mega", "defensive"]
    assert data["benchmarks"] == ["SPY"]
