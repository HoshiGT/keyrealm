# 数据库设计

## 存储位置

| 平台 | 路径 |
|------|------|
| Windows | `%APPDATA%/CredentialVault/credentials.db` |
| macOS | `~/Library/Application Support/CredentialVault/credentials.db` |
| Linux | `~/.local/share/credential-vault/credentials.db` |

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
| `schema_version` | 数据库版本号 | `"1"` |
| `salt` | PBKDF2 盐值（base64） | `"a3f2..."` |
| `verify_token` | 主密码验证密文（base64） | `"x9k1..."` |
| `mcp_port` | MCP Server 端口 | `"8765"` |
| `auto_verify` | 保存时自动验证 | `"true"` |

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
    credential_type  TEXT NOT NULL,             -- 插件类型标识，如 "llm_api_key"
    group_id         INTEGER,                   -- 所属分组（NULL = 未分组）
    data_encrypted   TEXT NOT NULL,             -- AES-GCM 加密的 JSON（敏感字段）
    data_plain       TEXT DEFAULT '{}',         -- 明文 JSON（非敏感字段，如厂商名）
    status           TEXT DEFAULT 'unknown',    -- valid / invalid / unknown / error
    status_detail    TEXT DEFAULT '',           -- 验证返回的账号信息（JSON）
    last_verified_at TIMESTAMP,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
  "vendor": "openai",
  "notes": "生产环境主账号"
}
```

> 设计原则：`data_encrypted` 存放密钥类敏感字段；`data_plain` 存放展示类非敏感字段，避免每次列表查询都需要解密。

---

### verification_history — 验证历史

```sql
CREATE TABLE verification_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id  INTEGER NOT NULL,
    status         TEXT NOT NULL,        -- valid / invalid / error
    result_data    TEXT DEFAULT '{}',    -- JSON：账号名、组织、余额等
    error_message  TEXT DEFAULT '',
    trigger_type   TEXT DEFAULT 'manual', -- manual / auto / scheduled
    duration_ms    INTEGER DEFAULT 0,
    verified_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (credential_id) REFERENCES credentials(id) ON DELETE CASCADE
);

CREATE INDEX idx_vhistory_credential ON verification_history(credential_id);
```

---

### plugins — 插件注册表

```sql
CREATE TABLE plugins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    author          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    credential_type TEXT NOT NULL UNIQUE,  -- 插件唯一标识
    fields_schema   TEXT NOT NULL,         -- JSON：字段定义（来自 manifest.json）
    plugin_path     TEXT NOT NULL,         -- 解压后的目录路径
    is_builtin      INTEGER DEFAULT 0,    -- 1=内置，0=用户安装
    enabled         INTEGER DEFAULT 1,
    installed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**fields_schema 示例**：
```json
[
  {
    "key": "base_url",
    "label": "Base URL",
    "type": "text",
    "required": true,
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
    "key": "vendor",
    "label": "厂商",
    "type": "select",
    "required": true,
    "options": ["openai", "anthropic", "doubao", "tongyi", "wenxin"],
    "sensitive": false
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

### mcp_permissions — MCP 工具权限

```sql
CREATE TABLE mcp_permissions (
    tool_name   TEXT PRIMARY KEY,
    enabled     INTEGER DEFAULT 0,   -- 默认关闭，需用户手动开启
    description TEXT DEFAULT '',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**初始工具列表**：

| tool_name | 默认 | 说明 |
|-----------|------|------|
| `vault_list_credentials` | ✅ 开 | 列出凭据（不含敏感字段明文） |
| `vault_get_credential` | ❌ 关 | 获取单条凭据详情 |
| `vault_create_credential` | ❌ 关 | 创建凭据 |
| `vault_update_credential` | ❌ 关 | 修改凭据 |
| `vault_delete_credential` | ❌ 关 | 删除凭据 |
| `vault_verify_credential` | ✅ 开 | 触发验证 |
| `vault_list_groups` | ✅ 开 | 列出分组 |
| `vault_create_group` | ❌ 关 | 创建分组 |
| `vault_list_plugins` | ✅ 开 | 列出插件 |
| `vault_install_plugin` | ❌ 关 | 安装插件 |
| `vault_get_secret` | ❌ 关 | 获取解密后的敏感字段（高危） |

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
        └── verification_history (credential_id FK)

plugins (credential_type ←→ credentials.credential_type)

mcp_permissions (tool_name)

schedules (target_id → groups.id 或 NULL)
```

---

## 迁移策略

`settings.schema_version` 记录当前版本。应用启动时：

```python
MIGRATIONS = {
    1: [],  # 初始版本
    # 2: [alter_table_sql, ...],
}

current = get_schema_version()
for v in range(current + 1, TARGET_VERSION + 1):
    for sql in MIGRATIONS[v]:
        execute(sql)
set_schema_version(TARGET_VERSION)
```
