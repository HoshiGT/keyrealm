#!/usr/bin/env bash
# 共享引导：确保项目下有一个可用的 Python（含虚拟环境与依赖）。**无交互**。
# 用法：
#   source "$(dirname "${BASH_SOURCE[0]}")/_bootstrap.sh"
#   ensure_python            # 成功后 $PY 指向可用的解释器
#   exec "$PY" <脚本> "$@"
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=""

ensure_python() {
  PY="$ROOT/.venv/bin/python"
  if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || command -v python || true)"
  fi
  if [ -z "${PY:-}" ]; then
    echo "未找到 Python，请先安装 Python 3.11+ 后重试" >&2
    return 2
  fi
  if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "创建虚拟环境 .venv …"
    "$PY" -m venv "$ROOT/.venv" || { echo "创建 venv 失败" >&2; return 2; }
    PY="$ROOT/.venv/bin/python"
  fi
  echo "安装 / 校验依赖 …"
  "$PY" -m pip install -q -r "$ROOT/requirements.txt" || { echo "安装 requirements 失败" >&2; return 2; }
  # PyInstaller 不在 requirements.txt 里，构建单独需要
  "$PY" -m pip install -q pyinstaller || { echo "安装 pyinstaller 失败" >&2; return 2; }
  return 0
}
