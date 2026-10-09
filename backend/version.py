"""应用版本与**数据库结构版本** —— 全项目**唯一事实来源**。

版本规则（2026-10-09 起生效）
----------------------------
- **修 bug / 做优化** → 末位 +1（patch，如 1.0.0 → 1.0.1）
- **增加特性 / 功能** → 中位 +1（minor，如 1.0.0 → 1.1.0）
- **较大型的框架变动** → 首位 +1（major，如 1.0.0 → 2.0.0）

**数据库结构有变动时，必须在 `scripts/` 附一份迁移脚本**（`migrate_*.py`），并把
``SCHEMA_VERSION`` 一起提升；应用本身**不再新增**自动迁移——`database._migrate()` 里那些
``from_version < N`` 只服务 1.0 之前的旧库，已冻结。

展示格式：**``1.0.0 (fe15e3b)``** —— 括号里是**构建时**的 commit 短 hash
（由 `scripts/build.py` 注入到 ``backend/_build_info.py``；源码运行时现查 git）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import is_frozen

# 应用版本（界面 / 日志展示用）
APP_VERSION = "1.0.0"

# 数据库结构版本：与应用同号起步。**只在结构变动时**随迁移脚本一起提升。
SCHEMA_VERSION = "1.0.0"

# 1.0 之前 schema_version 是整数（0..6）。读到**非整数**即说明是 1.0+ 的语义化版本，
# 那些旧的整数迁移一律视为已过（见 database._migrate）。
LEGACY_SCHEMA_MAX = 6

_ROOT = Path(__file__).resolve().parent.parent


def _git_commit() -> str:
    """现查 git 的 commit 短 hash；工作区脏时追加 ``-dirty``。取不到返回空串。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, cwd=str(_ROOT))
        if out.returncode != 0 or not out.stdout.strip():
            return ""
        value = out.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5, cwd=str(_ROOT))
        if dirty.returncode == 0 and dirty.stdout.strip():
            value += "-dirty"
        return value
    except Exception:  # noqa: BLE001 - 没装 git / 不是仓库
        return ""


def build_id() -> str:
    """构建标识。

    **源码运行**优先现查 git —— 这样本地构建留下的 ``_build_info.py`` **不会骗人**
    （它记的是上一次打包时的 hash）；**冻结的 exe 里没有 git**，才回落到构建时注入的值。
    """
    if not is_frozen():
        live = _git_commit()
        if live:
            return live
    try:
        from . import _build_info  # type: ignore[attr-defined]  # 由 scripts/build.py 生成
        value = str(getattr(_build_info, "COMMIT", "") or "").strip()
        if value:
            return value
    except Exception:  # noqa: BLE001 - 没生成过
        pass
    return _git_commit() or "unknown"


def version_string() -> str:
    """界面与日志统一用的一行：``1.0.0 (fe15e3b)``。"""
    return f"{APP_VERSION} ({build_id()})"
