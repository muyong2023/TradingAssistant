#!/usr/bin/env python3
"""提交前扫描：暂存区里出现 config/.env 的任何真实凭据就拒绝提交。

作为 git pre-commit 钩子运行（scripts/install_hooks.sh 负责安装）。
装它的原因：人工"跑一下检查脚本"靠不住 —— 有一次脚本确实报了警，
但命令链没有中止，提交照样发生了。检查必须由钩子强制执行。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "config" / ".env"
MIN_LEN = 8


def credentials() -> dict[str, str]:
    if not ENV.exists():
        return {}
    out = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= MIN_LEN:
                out[key.strip()] = value
    return out


def main() -> int:
    creds = credentials()
    if not creds:
        return 0
    staged = subprocess.run(["git", "diff", "--cached"],
                            capture_output=True, text=True).stdout
    hits = sorted({name for name, value in creds.items() if value in staged})
    if not hits:
        return 0

    print("提交被拒绝：暂存区里含有以下凭据的真实值\n", file=sys.stderr)
    for name in hits:
        value = creds[name]
        for path in subprocess.run(["git", "diff", "--cached", "--name-only"],
                                   capture_output=True, text=True).stdout.split():
            try:
                content = (ROOT / path).read_text(errors="replace")
            except OSError:
                continue
            for num, line in enumerate(content.splitlines(), 1):
                if value in line:
                    print(f"  {name}  ->  {path}:{num}", file=sys.stderr)
    print("\n把文件加进 .gitignore 或清除其中的凭据后重试。", file=sys.stderr)
    print("确需绕过：git commit --no-verify（请先确认真的安全）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
