#!/usr/bin/env bash
# 重启 Keyrealm（薄引导，**无交互**）。逻辑全在 scripts/restart.py。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_bootstrap.sh"
ensure_python || exit $?
exec "$PY" "$DIR/restart.py" "$@"
