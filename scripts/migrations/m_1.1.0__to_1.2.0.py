"""1.1.0 → 1.2.0：清掉几处数据库历史遗留。

三件事，**都不需要主密码**，因此能在交棒阶段用纯 SQL / Python 完成：

1. **删掉 `mcp_tokens` 表** —— 全局只有一个 Token，不需要生命周期（撤销 / 过期 / 最后使用时间），
   改由 `settings` 的两条键保存（`mcp_token_hash` / `mcp_token_prefix`）。这里把**现有活跃
   Token** 搬过去，用户不必重新生成、也不必重配 MCP 客户端。
2. **`plugins.plugin_path` 改存相对路径**（相对 `plugins/`，如 `llm/deepseek`）——早期写的是
   绝对路径，用户把程序目录搬一次就全失效（代码里至今留着兜底重定位）。
3. 其余表不动。

**凭据内部的数据格式**不在这里：那是 `backend/data_upgrades.py` 在**解锁后**做的事。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

FROM_VERSION = "1.1.0"
TO_VERSION = "1.2.0"

# 本脚本就在程序目录的 scripts/migrations/ 下，故 root = 上溯两级
_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGINS_DIR = _ROOT / "plugins"

_MCP_HASH_KEY = "mcp_token_hash"
_MCP_PREFIX_KEY = "mcp_token_prefix"


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def preview(conn) -> dict:
    """只读盘点：将删几条 token、将改几条绝对路径。"""
    tokens = 0
    if _has_table(conn, "mcp_tokens"):
        tokens = conn.execute("SELECT COUNT(*) AS c FROM mcp_tokens").fetchone()["c"]
    paths = sum(
        1 for r in conn.execute("SELECT plugin_path FROM plugins").fetchall()
        if os.path.isabs(r["plugin_path"] or "")
    )
    return {"mcp_tokens": tokens, "absolute_plugin_paths": paths}


def apply(conn) -> None:
    _migrate_mcp_token(conn)
    _relativize_plugin_paths(conn)
    conn.execute("DROP INDEX IF EXISTS idx_mcp_tokens_hash")
    conn.execute("DROP TABLE IF EXISTS mcp_tokens")


def _migrate_mcp_token(conn) -> None:
    """把当前活跃 Token（hash / 前缀）搬进 settings —— 搬完才删表。"""
    if not _has_table(conn, "mcp_tokens"):
        return
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT token_hash, token_prefix FROM mcp_tokens "
        "WHERE revoked=0 AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (now,),
    ).fetchone()
    if not row:
        return
    for key, value in ((_MCP_HASH_KEY, row["token_hash"]), (_MCP_PREFIX_KEY, row["token_prefix"])):
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
            (key, value, now),
        )


def _relativize_plugin_paths(conn) -> None:
    """`plugins.plugin_path`：绝对路径 → 相对 `plugins/` 的路径。"""
    for r in conn.execute("SELECT id, plugin_path FROM plugins").fetchall():
        raw = r["plugin_path"] or ""
        if not raw or not os.path.isabs(raw):
            continue
        try:
            rel = Path(raw).resolve().relative_to(_PLUGINS_DIR.resolve()).as_posix()
        except Exception:  # noqa: BLE001 - 对不上（目录已搬走）→ 退化成服务目录名
            rel = Path(raw).name
        conn.execute("UPDATE plugins SET plugin_path=? WHERE id=?", (rel, r["id"]))
