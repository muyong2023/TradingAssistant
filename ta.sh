#!/bin/bash
# 便捷入口：./ta.sh scan
cd "$(dirname "$0")" && exec .venv/bin/python -m ta.cli "$@"
