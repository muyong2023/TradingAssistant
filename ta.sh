#!/bin/bash
# 便捷入口：
#   ./ta.sh scan        命令行扫描
#   ./ta.sh web         启动本地看板 http://127.0.0.1:8787
cd "$(dirname "$0")" || exit 1
if [ "$1" = "web" ]; then
  shift
  exec .venv/bin/uvicorn ta.web.app:app --host 127.0.0.1 --port "${PORT:-8787}" "$@"
fi
exec .venv/bin/python -m ta.cli "$@"
