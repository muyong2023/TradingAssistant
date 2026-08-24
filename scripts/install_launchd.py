#!/usr/bin/env python3
"""生成并安装 launchd 定时任务。

    python3 scripts/install_launchd.py           # 只生成 plist 并打印
    python3 scripts/install_launchd.py --install # 生成并加载
    python3 scripts/install_launchd.py --remove  # 卸载

关于 Mac 睡眠：launchd 在唤醒后会补跑错过的 StartCalendarInterval 任务，
所以合盖一夜第二天仍会收到晨报（时间会晚一些）。要准点则需保持唤醒。
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.yongmu.tradingassistant"

PYTHON = ROOT / ".venv" / "bin" / "python"
LOGS = ROOT / "logs"

# 只在周一到周五触发（launchd 的 Weekday: 1=周一 … 5=周五）
WEEKDAYS = [1, 2, 3, 4, 5]


def calendar_job(job: str, hour: int, minute: int) -> dict:
    return {
        "Label": f"{PREFIX}.{job}",
        "ProgramArguments": [str(PYTHON), "-m", "ta.jobs", job],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": [
            {"Weekday": d, "Hour": hour, "Minute": minute} for d in WEEKDAYS
        ],
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.err.log"),
        "RunAtLoad": False,
    }


def interval_job(job: str, seconds: int) -> dict:
    # 每 N 秒无条件触发，由任务自己判断是否在交易时段内 ——
    # 比在 plist 里枚举 78 个时间点简单得多，也不会因夏令时错位。
    return {
        "Label": f"{PREFIX}.{job}",
        "ProgramArguments": [str(PYTHON), "-m", "ta.jobs", job],
        "WorkingDirectory": str(ROOT),
        "StartInterval": seconds,
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.err.log"),
        "RunAtLoad": False,
    }


def build() -> dict[str, dict]:
    return {
        "premarket": calendar_job("premarket", 9, 0),
        "intraday": interval_job("intraday", 300),
        "postclose": calendar_job("postclose", 16, 15),
    }


def write(plists: dict[str, dict]) -> list[Path]:
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    paths = []
    for job, data in plists.items():
        path = AGENTS / f"{PREFIX}.{job}.plist"
        path.write_bytes(plistlib.dumps(data))
        paths.append(path)
    return paths


def launchctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def domain() -> str:
    import os
    return f"gui/{os.getuid()}"


def install() -> int:
    if not PYTHON.exists():
        sys.exit(f"找不到 {PYTHON} —— 先建好 venv")
    paths = write(build())
    failed = False
    for path in paths:
        label = path.stem
        launchctl("bootout", f"{domain()}/{label}")      # 先卸载旧的，忽略失败
        code, out = launchctl("bootstrap", domain(), str(path))
        if code == 0:
            print(f"  ✓ {label}")
        else:
            failed = True
            print(f"  ✗ {label}: {out}")
    return 1 if failed else 0


def remove() -> int:
    for job in build():
        label = f"{PREFIX}.{job}"
        launchctl("bootout", f"{domain()}/{label}")
        path = AGENTS / f"{label}.plist"
        if path.exists():
            path.unlink()
        print(f"  已移除 {label}")
    return 0


def show() -> int:
    for job, data in build().items():
        print(f"\n--- {job} ---")
        if "StartInterval" in data:
            print(f"  每 {data['StartInterval']} 秒运行一次（任务内部判断交易时段）")
        else:
            iv = data["StartCalendarInterval"][0]
            print(f"  周一至周五 {iv['Hour']:02d}:{iv['Minute']:02d} 运行")
        print(f"  命令: {' '.join(data['ProgramArguments'])}")
        print(f"  日志: {data['StandardOutPath']}")
    print(f"\nplist 将写入 {AGENTS}")
    print("确认无误后运行：python3 scripts/install_launchd.py --install")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--install", action="store_true")
    p.add_argument("--remove", action="store_true")
    args = p.parse_args()
    if args.remove:
        return remove()
    if args.install:
        return install()
    return show()


if __name__ == "__main__":
    sys.exit(main())
