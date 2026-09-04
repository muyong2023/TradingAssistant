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
import re
import subprocess
import sys
import time
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


def daemon_job(job: str, module: str) -> dict:
    """常驻进程。KeepAlive 让 launchd 在它退出后自动拉起。"""
    return {
        "Label": f"{PREFIX}.{job}",
        "ProgramArguments": [str(PYTHON), "-m", module],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        #  崩溃立刻重启会打爆日志；给 10 秒退避
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.err.log"),
    }


def enabled_jobs() -> dict[str, bool]:
    """读 config.yaml 的 jobs 开关。关掉的任务干脆不装 ——
    装了再让进程每次唤醒后立刻退出是无谓的开销和日志噪音。

    手写解析而不用 PyYAML：这个脚本用系统 python3 运行（不在 venv 里），
    import yaml 会失败。先前 except 吞掉异常静默返回空表，
    结果开关形同虚设，已关闭的任务照装不误。
    """
    path = ROOT / "config" / "config.yaml"
    try:
        text = path.read_text()
    except OSError:
        return {}
    block = re.search(r"^jobs:\s*$(.*?)^\S", text, re.M | re.S)
    if not block:
        return {}
    flags: dict[str, bool] = {}
    for name, value in re.findall(r"^\s+(\w+):\s*(true|false)\b",
                                  block.group(1), re.M | re.I):
        flags[name] = value.lower() == "true"
    return flags


def web_job() -> dict:
    """本地看板。只监听回环地址——页面上是持仓且没有认证。"""
    return {
        "Label": f"{PREFIX}.web",
        "ProgramArguments": [str(ROOT / ".venv" / "bin" / "uvicorn"),
                             "ta.web.app:app", "--host", "127.0.0.1",
                             "--port", "8787"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOGS / "web.log"),
        "StandardErrorPath": str(LOGS / "web.err.log"),
    }


def checkpoints() -> list[tuple[int, int]]:
    """从 config.yaml 读巡检时刻。手写解析，理由同 enabled_jobs()。"""
    try:
        text = (ROOT / "config" / "config.yaml").read_text()
    except OSError:
        return []
    block = re.search(r"^checkpoints:\s*$(.*?)^\S", text, re.M | re.S)
    if not block:
        return []
    out = []
    for hh, mm in re.findall(r'^\s*-\s*"?(\d{1,2}):(\d{2})"?',
                             block.group(1), re.M):
        out.append((int(hh), int(mm)))
    return out


def multi_calendar_job(job: str, times: list[tuple[int, int]]) -> dict:
    """一个任务、多个触发时刻。"""
    return {
        "Label": f"{PREFIX}.{job}",
        "ProgramArguments": [str(PYTHON), "-m", "ta.jobs", job],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": [
            {"Weekday": d, "Hour": h, "Minute": m}
            for d in WEEKDAYS for h, m in times
        ],
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.err.log"),
        "RunAtLoad": False,
    }


def build() -> dict[str, dict]:
    flags = enabled_jobs()
    all_jobs = {
        "premarket": calendar_job("premarket", 9, 0),
        "intraday": interval_job("intraday", 300),
        "postclose": calendar_job("postclose", 16, 15),
        #  bot 常驻：即便问答关闭，它仍要接 /add /remove /list 等命令
        "bot": daemon_job("bot", "ta.bot"),
        "web": web_job(),
        "check": multi_calendar_job("check", checkpoints()),
    }
    return {name: spec for name, spec in all_jobs.items()
            if flags.get(name, True)}


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


def _wait_gone(label: str, timeout: float = 8.0) -> bool:
    """等到服务真正从 launchd 消失。

    bootout 是异步的：常驻任务（KeepAlive）的进程需要时间退出，
    此时立刻 bootstrap 会撞上 "Bootstrap failed: 5: Input/output error"。
    实测重装 bot 与 web 时必然复现。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = launchctl("print", f"{domain()}/{label}")
        if code != 0:
            return True
        time.sleep(0.3)
    return False


def install() -> int:
    if not PYTHON.exists():
        sys.exit(f"找不到 {PYTHON} —— 先建好 venv")
    plists = build()
    paths = write(plists)
    failed = False
    for path in paths:
        label = path.stem
        launchctl("bootout", f"{domain()}/{label}")
        _wait_gone(label)
        code, out = launchctl("bootstrap", domain(), str(path))
        if code != 0:
            #  偶发竞态：再等一轮重试一次
            _wait_gone(label)
            code, out = launchctl("bootstrap", domain(), str(path))
        if code != 0:
            failed = True
            print(f"  ✗ {label}: {out}")
            continue
        job = label[len(PREFIX) + 1:]
        if plists.get(job, {}).get("RunAtLoad"):
            #  bootstrap 之后 RunAtLoad 的任务常停在
            #  "pended nondemand spawn = speculative"：注册了但一直不启动，
            #  runs=0、连日志文件都不生成。踢一脚才真的跑起来。
            code, out = launchctl("kickstart", f"{domain()}/{label}")
            if code != 0:
                failed = True
                print(f"  ✗ {label}: 已装上但启动失败：{out}")
                continue
        print(f"  ✓ {label}")
    return 1 if failed else 0


def remove() -> int:
    #  卸载时不看开关，把所有可能装过的都清掉
    for job in ("premarket", "intraday", "postclose", "bot", "web", "check"):
        label = f"{PREFIX}.{job}"
        launchctl("bootout", f"{domain()}/{label}")
        path = AGENTS / f"{label}.plist"
        if path.exists():
            path.unlink()
        print(f"  已移除 {label}")
    return 0


def show() -> int:
    flags = enabled_jobs()
    skipped = [n for n, on in flags.items() if not on]
    if skipped:
        print(f"配置中已关闭、不会安装的任务：{', '.join(skipped)}")
    for job, data in build().items():
        print(f"\n--- {job} ---")
        if data.get("KeepAlive"):
            print("  常驻运行，退出后自动重启")
        elif len(data.get("StartCalendarInterval", [])) > len(WEEKDAYS):
            times = sorted({(i["Hour"], i["Minute"])
                            for i in data["StartCalendarInterval"]})
            print("  周一至周五 " + "、".join(f"{h:02d}:{m:02d}" for h, m in times))
        elif "StartInterval" in data:
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
