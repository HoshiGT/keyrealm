#!/usr/bin/env bash
# 数据库逐级迁移（薄引导，**无交互**）。逻辑全在 scripts/db_update.py。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_bootstrap.sh"
ensure_python || exit $?
exec "$PY" "$DIR/db_update.py" "$@"
