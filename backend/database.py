"""
SQLite 数据访问层
数据库位置：<app_dir>/data/credentials.db
（开发时 app_dir=项目根；打包冻结后 app_dir=exe 同级目录，保证可写持久化）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, Union

from .paths import app_dir

# 应用根目录：开发时=项目根；冻结后=exe 同级目录（保证可写、可持久化）
BASE_DIR = app_dir()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "credentials.db"

SCHEMA_VERSION = 5

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            color       TEXT DEFAULT '#409EFF',
            sort_order  INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS credentials (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            credential_type  TEXT NOT NULL,
            group_id         INTEGER,
            data_encrypted   TEXT NOT NULL,
            data_plain       TEXT DEFAULT '{}',
            status           TEXT DEFAULT 'unknown',
            status_detail    TEXT DEFAULT '{}',
            last_verified_at TIMESTAMP,
            last_valid_at    TIMESTAMP,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_credentials_type   ON credentials(credential_type);
        CREATE INDEX IF NOT EXISTS idx_credentials_group  ON credentials(group_id);
        CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials(status);

        CREATE TABLE IF NOT EXISTS plugins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            version         TEXT NOT NULL,
            author          TEXT DEFAULT '',
            description     TEXT DEFAULT '',
            credential_type TEXT NOT NULL UNIQUE,
            icon            TEXT DEFAULT '🔑',
            fields_schema   TEXT NOT NULL,
            plugin_path     TEXT NOT NULL,
            category        TEXT DEFAULT '',
            category_name   TEXT DEFAULT '',
            enabled         INTEGER DEFAULT 1,
            batch_verify    INTEGER DEFAULT 1,
            installed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS mcp_permissions (
            tool_name   TEXT PRIMARY KEY,
            enabled     INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS mcp_tokens (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            token_hash    TEXT NOT NULL UNIQUE,
            token_prefix  TEXT NOT NULL,
            tools_allowed TEXT DEFAULT '[]',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at  TIMESTAMP,
            expires_at    TIMESTAMP,
            revoked       INTEGER DEFAULT 0,
            revoked_at    TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);

        CREATE TABLE IF NOT EXISTS schedules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            trigger_type  TEXT NOT NULL,
            trigger_args  TEXT NOT NULL,
            target_scope  TEXT DEFAULT 'all',
            target_id     INTEGER,
            enabled       INTEGER DEFAULT 1,
            last_run_at   TIMESTAMP,
            next_run_at   TIMESTAMP,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 代理注册表：可维护多个代理、各自取名，其它地方一律按 id 引用。
        -- id 用 **UUID 而不是自增整数**：自增 id 只在单个库里唯一，一旦导出/导入（代理表是
        -- 「按地址去重」重建的）就对不上，凭据与预设里存的引用会**静默悬空 → 回落直连**，
        -- 界面上却看不出任何异常。UUID 跨库稳定，导入时原样沿用即可。
        CREATE TABLE IF NOT EXISTS proxies (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 类别级 / 插件级默认设置：新建凭据时按「插件 > 类别」继承（payload 是 JSON）
        -- 注意列名不能叫 values —— 那是 SQLite 的保留字，建表就会 syntax error
        CREATE TABLE IF NOT EXISTS preset_defaults (
            scope       TEXT NOT NULL,
            scope_key   TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (scope, scope_key)
        );
    """)
    conn.commit()
    _seed_mcp_permissions(conn)
    _migrate(conn)
    _set_schema_version(conn)


def _migrate(conn: sqlite3.Connection):
    """按 schema_version 执行增量迁移（在建表之后调用，ALTER 均有存在性检查）。"""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        from_version = int(row["value"]) if row else 0
    except Exception:
        from_version = 0

    if from_version < 2:
        # v2: credentials.last_valid_at、plugins.category/category_name
        cred_cols = {r["name"] for r in conn.execute("PRAGMA table_info(credentials)")}
        if "last_valid_at" not in cred_cols:
            conn.execute("ALTER TABLE credentials ADD COLUMN last_valid_at TIMESTAMP")

        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "category" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN category TEXT DEFAULT ''")
        if "category_name" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN category_name TEXT DEFAULT ''")

        # 最后有效时间回填：当前状态为有效的凭据，以最后验证时间近似
        conn.execute(
            "UPDATE credentials SET last_valid_at=last_verified_at "
            "WHERE status='valid' AND last_valid_at IS NULL"
        )
        conn.commit()

    if from_version < 3:
        # v3: plugins.batch_verify —— 无法自动化验证或有写副作用的插件禁用批量验证
        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "batch_verify" not in plugin_cols:
            conn.execute("ALTER TABLE plugins ADD COLUMN batch_verify INTEGER DEFAULT 1")
        conn.commit()

    if from_version < 4:
        # v4: 插件不再区分内置/外置，移除 plugins.is_builtin
        plugin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plugins)")}
        if "is_builtin" in plugin_cols:
            conn.execute("ALTER TABLE plugins DROP COLUMN is_builtin")
        conn.commit()

    if from_version < 5:
        # v5: 代理 id 由自增整数改成 UUID（见建表处的注释）。
        # 旧表 id 是 ``INTEGER PRIMARY KEY``（rowid 别名），**塞不进 uuid 文本**，必须重建。
        # 1.0 前不写数据迁移：旧的代理行直接丢弃，重加一次即可；引用旧 id 的凭据/预设
        # 会解析成「未配置」→ 回落直连，运行时有 WARNING 点名（见 plugin_manager._log_proxy_outlet）。
        cols = {r["name"]: str(r["type"] or "").upper() for r in conn.execute("PRAGMA table_info(proxies)")}
        if cols and cols.get("id") != "TEXT":
            conn.execute("DROP TABLE proxies")
            conn.executescript("""
                CREATE TABLE proxies (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    sort_order  INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("UPDATE settings SET value='' WHERE key='global_proxy_id'")
        conn.commit()


def _seed_mcp_permissions(conn: sqlite3.Connection):
    defaults = [
        ("vault_list_credentials",  1, "列出凭据（不含敏感字段明文）"),
        ("vault_get_credential",    0, "获取单条凭据详情（掩码）"),
        ("vault_create_credential", 0, "创建凭据"),
        ("vault_update_credential", 0, "修改凭据"),
        ("vault_delete_credential", 0, "删除凭据"),
        ("vault_verify_credential", 1, "触发验证"),
        ("vault_batch_verify",      0, "批量验证"),
        ("vault_list_groups",       1, "列出分组"),
        ("vault_create_group",      0, "创建分组"),
        ("vault_list_plugins",      1, "列出插件"),
        ("vault_get_secret",        0, "获取解密后的敏感字段（高危）"),
        ("vault_export_backup",     0, "导出备份（触及整库数据，高危；encrypted=false 会写出明文密钥）"),
        ("vault_import_backup",     0, "导入备份（合并入库，高危：会新增凭据、可能覆盖偏好设置）"),
    ]
    for tool, enabled, desc in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO mcp_permissions (tool_name, enabled, description) VALUES (?,?,?)",
            (tool, enabled, desc),
        )
    # 权限表以 defaults 为准：清理已不存在的工具记录
    placeholders = ",".join("?" for _ in defaults)
    conn.execute(
        f"DELETE FROM mcp_permissions WHERE tool_name NOT IN ({placeholders})",
        [tool for tool, _, _ in defaults],
    )
    conn.commit()


def _set_schema_version(conn: sqlite3.Connection):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('schema_version', ?, ?)",
        (str(SCHEMA_VERSION), _now()),
    )
    conn.commit()


# ── settings ──────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with _lock:
        row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with _lock:
        get_conn().execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
            (key, value, _now()),
        )
        get_conn().commit()


# ── groups ────────────────────────────────────────────────

def list_groups() -> list[dict]:
    with _lock:
        rows = get_conn().execute("""
            SELECT g.*, COUNT(c.id) as credential_count
            FROM groups g
            LEFT JOIN credentials c ON c.group_id = g.id
            GROUP BY g.id
            ORDER BY g.sort_order, g.name
        """).fetchall()
    return [dict(r) for r in rows]


def create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO groups (name, description, color, created_at) VALUES (?,?,?,?)",
            (name, description, color, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def update_group(group_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "description", "color", "sort_order"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return None
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [group_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE groups SET {sets} WHERE id=?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    return dict(row) if row else None


def delete_group(group_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.commit()


# ── credentials ───────────────────────────────────────────

def list_credentials(
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    credential_types: Optional[list] = None,
) -> list[dict]:
    sql = """
        SELECT c.*, g.name as group_name
        FROM credentials c
        LEFT JOIN groups g ON g.id = c.group_id
        WHERE 1=1
    """
    params: list = []
    if group_id is not None:
        sql += " AND c.group_id=?"
        params.append(group_id)
    if credential_type:
        sql += " AND c.credential_type=?"
        params.append(credential_type)
    if credential_types:
        placeholders = ",".join("?" for _ in credential_types)
        sql += f" AND c.credential_type IN ({placeholders})"
        params += list(credential_types)
    if status:
        sql += " AND c.status=?"
        params.append(status)
    if search:
        sql += " AND (c.name LIKE ? OR c.data_plain LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY c.updated_at DESC"
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["data_plain"] = json.loads(d.get("data_plain") or "{}")
        d["status_detail"] = json.loads(d.get("status_detail") or "{}")
        results.append(d)
    return results


def get_credential(cred_id: int) -> Optional[dict]:
    with _lock:
        row = get_conn().execute("""
            SELECT c.*, g.name as group_name
            FROM credentials c
            LEFT JOIN groups g ON g.id = c.group_id
            WHERE c.id=?
        """, (cred_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["data_plain"] = json.loads(d.get("data_plain") or "{}")
    d["status_detail"] = json.loads(d.get("status_detail") or "{}")
    return d


def create_credential(
    name: str,
    credential_type: str,
    data_encrypted: str,
    data_plain: dict,
    group_id: Optional[int] = None,
) -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO credentials
               (name, credential_type, group_id, data_encrypted, data_plain, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, credential_type, group_id, data_encrypted,
             json.dumps(data_plain, ensure_ascii=False), _now(), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM credentials WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["data_plain"] = json.loads(d.get("data_plain") or "{}")
    return d


def update_credential(cred_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "group_id", "data_encrypted", "data_plain", "status", "status_detail",
               "last_verified_at", "last_valid_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "data_plain" in fields and isinstance(fields["data_plain"], dict):
        fields["data_plain"] = json.dumps(fields["data_plain"], ensure_ascii=False)
    if "status_detail" in fields and isinstance(fields["status_detail"], dict):
        fields["status_detail"] = json.dumps(fields["status_detail"], ensure_ascii=False)
    if not fields:
        return None
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [cred_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE credentials SET {sets} WHERE id=?", vals)
        conn.commit()
    return get_credential(cred_id)


def delete_credential(cred_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM credentials WHERE id=?", (cred_id,))
        conn.commit()


# ── plugins ───────────────────────────────────────────────

def list_plugins(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM plugins"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY name"
    with _lock:
        rows = get_conn().execute(sql).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["fields_schema"] = json.loads(d.get("fields_schema") or "[]")
        results.append(d)
    return results


def get_plugin_by_type(credential_type: str) -> Optional[dict]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM plugins WHERE credential_type=? AND enabled=1",
            (credential_type,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["fields_schema"] = json.loads(d.get("fields_schema") or "[]")
    return d


def upsert_plugin(
    name: str, version: str, credential_type: str, fields_schema: list,
    plugin_path: str, author: str = "", description: str = "",
    icon: str = "🔑",
    category: str = "", category_name: str = "", batch_verify: bool = True,
):
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT INTO plugins (name, version, author, description, credential_type,
                                    icon, fields_schema, plugin_path, category, category_name,
                                    batch_verify, enabled, installed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(credential_type) DO UPDATE SET
                 name=excluded.name, version=excluded.version,
                 author=excluded.author, description=excluded.description,
                 icon=excluded.icon, fields_schema=excluded.fields_schema,
                 plugin_path=excluded.plugin_path,
                 category=excluded.category, category_name=excluded.category_name,
                 batch_verify=excluded.batch_verify,
                 enabled=1""",
            (name, version, author, description, credential_type, icon,
             json.dumps(fields_schema, ensure_ascii=False), plugin_path,
             category, category_name or category, int(batch_verify), _now()),
        )
        conn.commit()


def delete_plugin_record(credential_type: str):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM plugins WHERE credential_type=?", (credential_type,))
        conn.commit()


# ── mcp_permissions ───────────────────────────────────────

def list_mcp_permissions() -> list[dict]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM mcp_permissions ORDER BY tool_name").fetchall()
    return [dict(r) for r in rows]


def set_mcp_permission(tool_name: str, enabled: bool):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE mcp_permissions SET enabled=?, updated_at=? WHERE tool_name=?",
            (int(enabled), _now(), tool_name),
        )
        conn.commit()


def is_tool_enabled(tool_name: str) -> bool:
    with _lock:
        row = get_conn().execute(
            "SELECT enabled FROM mcp_permissions WHERE tool_name=?", (tool_name,)
        ).fetchone()
    return bool(row and row["enabled"])


# ── mcp_tokens ────────────────────────────────────────────
# MCP 只保留【单个活跃 Token】：轮换时先撤销已有活跃 Token，再签发新的。

def get_active_mcp_token() -> Optional[dict]:
    """返回当前活跃（未撤销且未过期）的 Token，取最新创建的一个；无则 None。"""
    with _lock:
        row = get_conn().execute(
            """SELECT * FROM mcp_tokens
               WHERE revoked=0 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (_now(),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tools_allowed"] = json.loads(d.get("tools_allowed") or "[]")
    return d


def create_mcp_token(
    name: str, token_hash: str, token_prefix: str,
    tools_allowed: list[str] = None, expires_at: Optional[str] = None,
) -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO mcp_tokens (name, token_hash, token_prefix, tools_allowed, created_at, expires_at)
               VALUES (?,?,?,?,?,?)""",
            (name, token_hash, token_prefix,
             json.dumps(tools_allowed or [], ensure_ascii=False), _now(), expires_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM mcp_tokens WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["tools_allowed"] = json.loads(d.get("tools_allowed") or "[]")
    return d


def get_token_by_hash(token_hash: str) -> Optional[dict]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM mcp_tokens WHERE token_hash=? AND revoked=0", (token_hash,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tools_allowed"] = json.loads(d.get("tools_allowed") or "[]")
    return d


def touch_token(token_hash: str):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE mcp_tokens SET last_used_at=? WHERE token_hash=?", (_now(), token_hash))
        conn.commit()


def revoke_mcp_token(token_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE mcp_tokens SET revoked=1, revoked_at=? WHERE id=?", (_now(), token_id))
        conn.commit()


def revoke_active_mcp_tokens() -> int:
    """撤销所有活跃 Token（轮换用，保证全局至多一个活跃 Token）。返回撤销数量。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "UPDATE mcp_tokens SET revoked=1, revoked_at=? WHERE revoked=0", (_now(),)
        )
        conn.commit()
    return cur.rowcount or 0


def update_token_scope(token_id: int, tools_allowed: list[str]):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE mcp_tokens SET tools_allowed=? WHERE id=?",
            (json.dumps(tools_allowed, ensure_ascii=False), token_id),
        )
        conn.commit()


def count_active_tokens() -> int:
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) as cnt FROM mcp_tokens WHERE revoked=0 AND (expires_at IS NULL OR expires_at > ?)",
            (_now(),),
        ).fetchone()
    return row["cnt"] if row else 0


# ── schedules ─────────────────────────────────────────────

def list_schedules() -> list[dict]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM schedules ORDER BY created_at").fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["trigger_args"] = json.loads(d.get("trigger_args") or "{}")
        results.append(d)
    return results


def create_schedule(
    name: str, trigger_type: str, trigger_args: dict,
    target_scope: str = "all", target_id: Optional[Union[int, str]] = None,
) -> dict:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO schedules (name, trigger_type, trigger_args, target_scope, target_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (name, trigger_type, json.dumps(trigger_args), target_scope, target_id, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["trigger_args"] = json.loads(d.get("trigger_args") or "{}")
    return d


def update_schedule(schedule_id: int, **kwargs):
    allowed = {"name", "trigger_type", "trigger_args", "target_scope", "target_id", "enabled", "last_run_at", "next_run_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "trigger_args" in fields and isinstance(fields["trigger_args"], dict):
        fields["trigger_args"] = json.dumps(fields["trigger_args"])
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [schedule_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE schedules SET {sets} WHERE id=?", vals)
        conn.commit()


def delete_schedule(schedule_id: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        conn.commit()


# ── 代理注册表 ────────────────────────────────────────────

def list_proxies() -> list[dict]:
    with _lock:
        rows = get_conn().execute(
            # 按**创建时间**排，不能按 id：id 是 UUID，字典序等于随机序，
            # 用它当 tie-break 会让界面上的代理列表每次刷新都换位置。
            "SELECT * FROM proxies ORDER BY sort_order, created_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_proxy(proxy_id: int) -> Optional[dict]:
    with _lock:
        row = get_conn().execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
    return dict(row) if row else None


def create_proxy(name: str, url: str, proxy_id: Optional[str] = None) -> dict:
    """新建代理。``proxy_id`` 用于**导入备份时原样沿用源库的 UUID**（默认现生成一个）。"""
    pid = str(proxy_id or "").strip() or uuid.uuid4().hex
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO proxies (id, name, url, created_at) VALUES (?,?,?,?)",
            (pid, name, url, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM proxies WHERE id=?", (pid,)).fetchone()
    return dict(row)


def update_proxy(proxy_id: int, **kwargs) -> Optional[dict]:
    allowed = {"name", "url", "sort_order"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return get_proxy(proxy_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [proxy_id]
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE proxies SET {sets} WHERE id=?", vals)
        conn.commit()
    return get_proxy(proxy_id)


def delete_proxy(proxy_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        conn.commit()
    return cur.rowcount > 0


# ── 类别级 / 插件级默认设置 ────────────────────────────────

def get_preset_defaults(scope: str, scope_key: str) -> dict:
    """读取某一作用域的默认设置（JSON 对象）；不存在时返回 {}。"""
    with _lock:
        row = get_conn().execute(
            "SELECT payload FROM preset_defaults WHERE scope=? AND scope_key=?",
            (scope, scope_key),
        ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["payload"] or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def list_preset_defaults() -> list[dict]:
    """全部默认设置记录（备份导出用）。"""
    with _lock:
        rows = get_conn().execute("SELECT * FROM preset_defaults").fetchall()
    return [dict(r) for r in rows]


def set_preset_defaults(scope: str, scope_key: str, values: dict) -> dict:
    """覆盖写入某一作用域的默认设置。"""
    payload = json.dumps(values or {}, ensure_ascii=False)
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO preset_defaults (scope, scope_key, payload, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(scope, scope_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (scope, scope_key, payload, _now()),
        )
        conn.commit()
    return values or {}


def delete_preset_defaults(scope: str, scope_key: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM preset_defaults WHERE scope=? AND scope_key=?", (scope, scope_key))
        conn.commit()
