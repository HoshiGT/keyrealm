#!/usr/bin/env bash
# 一键更新：备份 → 取源码 → 编译 → 数据库迁移 → 重启（薄引导，**无交互**）。
# 逻辑全在 scripts/build_update.py（运行日志自动落 data/logs/build_update-*.log）。
# 无「准备产物」时它走**完整流程**（自行下载/拉源码/构建）；应用内「立即更新」则改用
# prepare_update（应用内准备）+ main.py --self-update（交棒），不走本脚本。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_bootstrap.sh"
ensure_python || exit $?
exec "$PY" "$DIR/build_update.py" "$@"
