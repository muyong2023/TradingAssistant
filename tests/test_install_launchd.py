"""安装脚本的解析逻辑测试。该脚本用系统 python3 运行，不能依赖第三方库。"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location(
        "install_launchd", ROOT / "scripts" / "install_launchd.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_reads_job_flags_without_yaml(mod):
    """曾因 import yaml 失败被 except 吞掉，开关形同虚设、关掉的任务照装。"""
    flags = mod.enabled_jobs()
    assert flags, "应当解析出至少一个开关"
    assert all(isinstance(v, bool) for v in flags.values())


def test_disabled_jobs_not_built(mod):
    flags = mod.enabled_jobs()
    built = mod.build()
    for name, on in flags.items():
        if not on:
            assert name not in built, f"{name} 已关闭却仍被安装"


def test_reads_checkpoints(mod):
    times = mod.checkpoints()
    assert (7, 0) in times and (9, 30) in times
    assert all(0 <= h < 24 and 0 <= m < 60 for h, m in times)


def test_check_job_has_entry_per_weekday_and_time(mod):
    spec = mod.build().get("check")
    if spec is None:
        pytest.skip("check 未启用")
    entries = spec["StartCalendarInterval"]
    assert len(entries) == len(mod.WEEKDAYS) * len(mod.checkpoints())
    assert {e["Weekday"] for e in entries} == set(mod.WEEKDAYS)


def test_every_job_points_at_venv_python(mod):
    for name, spec in mod.build().items():
        program = spec["ProgramArguments"][0]
        assert ".venv" in program, f"{name} 未使用 venv 解释器"
