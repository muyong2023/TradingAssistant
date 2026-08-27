#!/bin/bash
# 安装 git 钩子。克隆仓库后运行一次。
set -e
cd "$(dirname "$0")/.."
mkdir -p .git/hooks
cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/bash
exec python3 "$(git rev-parse --show-toplevel)/scripts/check_secrets.py"
HOOK
chmod +x .git/hooks/pre-commit
echo "已安装 pre-commit 钩子：提交前自动扫描凭据"
