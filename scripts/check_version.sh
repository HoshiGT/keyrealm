#!/usr/bin/env bash
# 检查更新（薄引导，**无交互**）。逻辑全在 scripts/check_version.py。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_bootstrap.sh"
ensure_python || exit $?
exec "$PY" "$DIR/check_version.py" "$@"
