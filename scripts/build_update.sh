#!/usr/bin/env bash
# 一键更新：备份 → 取源码 → 编译 → 数据库迁移 → 重启（薄引导，**无交互**）。
# 逻辑全在 scripts/build_update.py。应用内「立即更新」就是脱离进程跑本脚本。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_bootstrap.sh"
ensure_python || exit $?
exec "$PY" "$DIR/build_update.py" "$@"
