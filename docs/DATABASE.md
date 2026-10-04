# 数据库设计

## 存储位置

数据库存放在**项目目录内**，避免跨目录污染：

```
API-Key-Checker/
└── data/
    ├── credentials.db        # 主数据库
    ├── credentials.db-wal    # WAL 日志（运行时）
    └── credentials.db-shm    # 共享内存（运行时）
```

> `data/` 目录已加入 `.gitignore`，不会提交到版本库。应用启动时自动创建。

SQLite 开启 WAL 模式，支持读写并发。

---

## 表结构

### settings — 全局配置

```sql
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

| key | 说明 | 示例值 |
|-----|------|--------|
| `schema_version` | 数据库版本号 | `"4"` |
| `salt` | PBKDF2 盐值（base64） | `"a3f2..."` |
| `verify_token` | 主密码验证密文（base64） | `"x9k1..."` |
| `mcp_port` | MCP Server 端口 | `"8765"` |
| `auto_verify` | 保存时自动验证 | `"true"` |
| `log_level` | 日志级别 | `"INFO"` |
| `verify_proxy` | 验证代理（空 = 直连；http/https/socks5） | `"socks5://127.0.0.1:10808"` |
| `close_action` | 关闭主窗口行为 | `"ask"` |
| `close_action` | 关闭窗口行为（ask / quit / background） | `"ask"` |

---

### groups — 凭据分组

```sql
CREATE TABLE groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    color       TEXT DEFAULT '#409EFF',   -- 前端标签颜色
    sort_order  INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### credentials — 凭据主表

```sql
CREATE TABLE credentials (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,             -- 用户自定义名称（明文）
    credential_type  TEXT NOT NULL,             -- 插件类型标识，如 "llm_openai"、"api_firecrawl"
    group_id         INTEGER,                   -- 所属分组（NULL = 未分组）
    data_encrypted   TEXT NOT NULL,             -- AES-GCM 加密的 JSON（敏感字段）
    data_plain       TEXT DEFAULT '{}',         -- 明文 JSON（非敏感字段，如备注）
    status           TEXT DEFAULT 'unknown',    -- valid / invalid / unknown / error
    status_detail    TEXT DEFAULT '',           -- 验证返回的账号信息（JSON，如余额）
    last_verified_at TIMESTAMP,                 -- 最后一次验证时间（无论结果）
    last_valid_at    TIMESTAMP,                 -- 最后一次验证通过时间（v2 新增）
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 添加时间
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
);

CREATE INDEX idx_credentials_type   ON credentials(credential_type);
CREATE INDEX idx_credentials_group  ON credentials(group_id);
CREATE INDEX idx_credentials_status ON credentials(status);
```

**data_encrypted 示例**（解密后）：
```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxx",
  "base_url": "https://api.openai.com/v1"
}
```

**data_plain 示例**：
```json
{
  "notes": "生产环境主账号"
}
```

> 设计原则：`data_encrypted` 存放密钥类敏感字段；`data_plain` 存放展示类非敏感字段，避免每次列表查询都需要解密。
> 不再维护独立的验证历史表：只保留 `created_at`（添加时间）、`last_verified_at`、`last_valid_at` 三个时间戳。

---

### plugins — 插件注册表

```sql
CREATE TABLE plugins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    author          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    credential_type TEXT NOT NULL UNIQUE,  -- 插件唯一标识，如 "llm_openai"
    icon            TEXT DEFAULT '🔑',     -- emoji 或插件目录内图片文件名（如 logo.png）
    fields_schema   TEXT NOT NULL,         -- JSON：字段定义（来自 manifest.json）
    plugin_path     TEXT NOT NULL,         -- 插件目录路径（plugins/<类别>/<服务>/）
    category        TEXT DEFAULT '',       -- 类别目录名（llm / api / dns / …，空 = 根级平铺插件）
    category_name   TEXT DEFAULT '',       -- 类别显示名（来自 category.json）
    enabled         INTEGER DEFAULT 1,
    batch_verify    INTEGER DEFAULT 1,    -- 1=参与批量验证；0=禁用（仅手动，v3 新增）
    installed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

> 插件不再区分「内置 / 外置」，全部来自 `plugins/` 目录。

**fields_schema 示例**（llm_openai 子插件）：
```json
[
  {
    "key": "base_url",
    "label": "Base URL",
    "type": "url",
    "required": false,
    "default": "https://api.openai.com/v1",
    "sensitive": false
  },
  {
    "key": "api_key",
    "label": "API Key",
    "type": "password",
    "required": true,
    "sensitive": true
  },
  {
    "key": "notes",
    "label": "备注",
    "type": "textarea",
    "required": false,
    "sensitive": false
  }
]
```

---

### mcp_permissions — MCP 工具全局权限

```sql
CREATE TABLE mcp_permissions (
    tool_name   TEXT PRIMARY KEY,
    enabled     INTEGER DEFAULT 0,   -- 默认关闭，需用户手动开启
    description TEXT DEFAULT '',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**初始工具列表**（共 11 个）：

| tool_name | 默认 | 说明 |
|-----------|------|------|
| `vault_list_credentials` | ✅ 开 | 列出凭据（不含敏感字段明文） |
| `vault_get_credential` | ❌ 关 | 获取单条凭据详情 |
| `vault_create_credential` | ❌ 关 | 创建凭据 |
| `vault_update_credential` | ❌ 关 | 修改凭据 |
| `vault_delete_credential` | ❌ 关 | 删除凭据 |
| `vault_verify_credential` | ✅ 开 | 触发验证 |
| `vault_batch_verify` | ❌ 关 | 批量验证 |
| `vault_list_groups` | ✅ 开 | 列出分组 |
| `vault_create_group` | ❌ 关 | 创建分组 |
| `vault_list_plugins` | ✅ 开 | 列出插件 |
| `vault_get_secret` | ❌ 关 | 获取解密后的敏感字段（高危） |

---

### mcp_tokens — MCP 访问令牌

```sql
CREATE TABLE mcp_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,             -- Token 备注名，如 "Claude Desktop"
    token_hash    TEXT NOT NULL UNIQUE,      -- SHA-256(token)，不存明文
    token_prefix  TEXT NOT NULL,             -- Token 前 8 位，用于列表展示（如 "cv_a3f2"）
    tools_allowed TEXT DEFAULT '[]',         -- JSON 数组：允许调用的工具名；空数组 = 继承全局权限
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at  TIMESTAMP,
    expires_at    TIMESTAMP,                 -- NULL = 永不过期
    revoked       INTEGER DEFAULT 0,         -- 1 = 已吊销
    revoked_at    TIMESTAMP
);

CREATE INDEX idx_mcp_tokens_hash ON mcp_tokens(token_hash);
```

**Token 格式**：`cv_` + 32 位随机十六进制，共 35 字符。

```
cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7
   └────────────── 32 hex ──────────────┘
```

**鉴权流程**：
```
Agent 请求 Header: Authorization: Bearer cv_a3f2...
    │
    ▼ SHA-256("cv_a3f2...") → token_hash
    │
    ▼ SELECT * FROM mcp_tokens WHERE token_hash=? AND revoked=0
    │
    ├── 未找到 → 401
    ├── expires_at < now() → 401（Token 已过期）
    └── 找到 → 检查 tools_allowed ∩ mcp_permissions → 允许/拒绝
```

**tools_allowed 示例**：
```json
["vault_list_credentials", "vault_verify_credential", "vault_list_groups"]
```
> 空数组 `[]` 表示继承全局 `mcp_permissions` 中所有已开启的工具。

---

### schedules — 定时验证计划

```sql
CREATE TABLE schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    trigger_type  TEXT NOT NULL,     -- interval / cron
    trigger_args  TEXT NOT NULL,     -- JSON：{"hours": 24} 或 {"cron": "0 2 * * *"}
    target_scope  TEXT DEFAULT 'all', -- all / group / type
    target_id     INTEGER,           -- group_id 或 credential_type
    enabled       INTEGER DEFAULT 1,
    last_run_at   TIMESTAMP,
    next_run_at   TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## ER 图

```
settings (key-value)

groups
  └── credentials (group_id FK)

plugins (credential_type ←→ credentials.credential_type)

mcp_permissions (tool_name)          ← 全局工具开关
mcp_tokens (tools_allowed JSON)      ← Token 级范围，与 mcp_permissions 取交集

schedules (target_id → groups.id 或 NULL)
```

---

## 迁移策略

`settings.schema_version` 记录当前版本（当前为 **4**）。应用启动时按版本号顺序执行迁移：

- **v1 → v2**：`credentials` 新增 `last_valid_at`；`plugins` 新增 `category` / `category_name`；
  凭据类型从整包命名（`llm_api_key` / `api_service`）迁移为子插件命名
  （`llm_<vendor>` / `api_<service>`）；回填 `last_valid_at`；删除 `verification_history` 表；
  移除 `vault_install_plugin` 权限记录。
- **v2 → v3**：`plugins` 新增 `batch_verify`（默认 1）；`settings` 新增 `verify_proxy`（验证代理）。
- **v3 → v4**：插件不再区分内置/外置，移除 `plugins.is_builtin`。
