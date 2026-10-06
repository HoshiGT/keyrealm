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
| `schema_version` | 数据库版本号 | `"6"` |
| `salt` | PBKDF2 盐值（base64） | `"a3f2..."` |
| `verify_token` | 主密码验证密文（base64） | `"x9k1..."` |
| `mcp_host` | MCP Server 监听地址（仅 IP / localhost，默认 `127.0.0.1`） | `"127.0.0.1"` |
| `mcp_port` | MCP Server 监听端口（1–65535，默认 `8765`；被占用时报错不自动换） | `"8765"` |
| `auto_verify` | 保存时自动验证 | `"true"` |
| `log_level` | 日志级别 | `"INFO"` |
| `verify_proxy` | **全局代理地址的快照**（由 `global_proxy_id` 解析后写入，供 `VAULT_PROXY` 环境变量那条老通道用） | `"socks5://127.0.0.1:10808"` |
| `global_proxy_id` | 全局默认代理在 `proxies` 表里的 id（**UUID 字符串**，空 = 直连） | `"a1eb5c2ac79c42af96011e770b7d78ca"` |
| `batch_concurrency` | 批量验证并发线程数（1–32，空 = 默认 8） | `"8"` |
| `verify_timeout` | 验证请求超时秒数（5–300，空 = 默认 20） | `"20"` |
| `close_action` | 关闭主窗口行为（ask / quit / background） | `"ask"` |
| `auto_lock_minutes` | 空闲自动锁定的分钟数（0 = 关闭；仅允许 0/1/5/10/15/30/60，其余按 0 处理） | `"0"` |

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

> **LLM 类**凭据还会在 `data_plain` 里带两个**可选**字段（非敏感，逐条凭据独立配置）：
> `probe_chat`（布尔，余额未知时是否发一次最小对话探测欠费，默认 false）与
> `probe_model`（字符串，探测模型，空 = 自动选）。见 `docs/PLUGIN_GUIDE.md` 第 2.4 节。
>
> **所有类别**的凭据还可能带两个代理保留键（同样非敏感）：`_proxy_mode`
> （`global` / `proxy` / `direct`）与 `_proxy_id`（`mode=proxy` 时有效）。
> 放进 `data` 而不另建表，是为了让四条验证路径（单条 / 批量 / MCP / 流式）都不必改签名
> ——`data` 本来就会原样交到插件手里。见 `docs/ARCHITECTURE.md` 的「按调用生效的代理」。

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
    enabled         INTEGER DEFAULT 1,    -- plugins/.state.json 的只读镜像（不是功能闸门，见「迁移策略」）
    batch_verify    INTEGER DEFAULT 1,    -- 1=参与批量验证；0=禁用（仅手动，v3 新增）
    installed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

> 插件不再区分「内置 / 外置」，全部来自 `plugins/` 目录。
> **`plugins.enabled` 只是 `plugins/.state.json` 的只读镜像**：插件启用/禁用由该状态文件决定，
> `sync_plugins()` 每次扫描把它镜像进这一列；`get_plugin_by_type()` 查询**不按 `enabled` 过滤**，
> 真正的功能闸门是 `plugin_manager.get_module()`（禁用即返回 `None`）。

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

### proxies — 代理注册表

```sql
CREATE TABLE IF NOT EXISTS proxies (
    id          TEXT PRIMARY KEY,       -- UUID（见下）；**不是**自增整数
    name        TEXT NOT NULL,          -- 用户取的名字，仅用于展示
    url         TEXT NOT NULL,          -- http:// / https:// / socks4:// / socks5:// / socks5h://
    sort_order  INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

> **其它模块一律按 `id` 引用代理**，不传名字、也不传地址：全局默认存
> `settings.global_proxy_id`，每条凭据存自己 `data` 里的 `_proxy_mode` / `_proxy_id`。
> 删除某个代理后，引用它的凭据会在解析时回退到全局（不报错）。
>
> **id 为什么用 UUID 而不是自增整数**：自增只在单个库里唯一。备份导入时代理是**原样沿用
> 源库 id** 重建的，一旦 id 变了，凭据/预设里的 `_proxy_id` 就会静默悬空 → 回落直连
> ——界面上代理名还好好显示着，症状是「配了代理却完全不生效」。UUID 跨库稳定，这类问题从根上消失。
> 老库（整型主键）在 `init_db` 的 v5 迁移里**重建该表**（旧行丢弃，`global_proxy_id` 一并清空；
> 1.0 前不做数据迁移，重加一次代理即可）。

---

### preset_defaults — 类别 / 插件默认设置

```sql
CREATE TABLE IF NOT EXISTS preset_defaults (
    scope       TEXT NOT NULL,          -- 'category' | 'plugin'
    scope_key   TEXT NOT NULL,          -- 类别目录名（llm / dns / …）或 credential_type
    values      TEXT NOT NULL DEFAULT '{}',   -- JSON：{"base_url": "...", "_proxy_mode": "direct", ...}
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, scope_key)
);
```

> 新建凭据时的预填值，按「**插件级 > 类别级 > manifest 的 field.default**」继承。
> `values` 是**明文列**，因此敏感字段永远不写入（写入 / 读取 / 注入三处各剔一次，
> 见 `backend/preset_store.py`）。键里可以带代理保留键 `_proxy_mode` / `_proxy_id`。
> 继承在**读取时**按目标插件的可预设字段再过滤一次：类别级设了某键、但该子插件没有这个
> 字段时会自动丢掉，不会把未知键塞进凭据数据。

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

**初始工具列表**（共 13 个）：

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
| `vault_export_backup` | ❌ 关 | 导出备份（高危；`encrypted=false` 会把密钥明文写出） |
| `vault_import_backup` | ❌ 关 | 导入备份（高危：会合并凭据、可能覆盖偏好设置） |

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
> 当前界面与 API 均未提供设置该字段的入口，实际恒为 `[]`（= 继承全局）；鉴权层保留该能力。

---

### schedules — 定时验证计划

```sql
CREATE TABLE schedules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    trigger_type   TEXT NOT NULL,     -- interval / cron
    trigger_args   TEXT NOT NULL,     -- JSON：{"hours": 24} 或 {"cron": "0 2 * * *"}
    target_scope   TEXT DEFAULT 'all', -- all / group / type / credentials
    target_id      INTEGER,           -- group_id 或 credential_type（scope=credentials 时为 NULL）
    credential_ids TEXT DEFAULT '[]', -- scope='credentials' 时勾选的具体凭据 id，JSON 数组；其它 scope 为空数组
    enabled        INTEGER DEFAULT 1,
    last_run_at    TIMESTAMP,
    next_run_at    TIMESTAMP,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

> `target_id` 是**多态字段**：`scope='group'` 存分组 id（int），`scope='type'` 存凭据类型名（str）。
> `credential_ids` 只在 `scope='credentials'` 时有值，由前端「目录树勾选」得到（与「备份 → 仅选中的凭据」
> 用同一个组件 `CredentialTreePicker.vue`）。DAO 层读写都经 `_parse_id_list()` 归一为 **int 列表**
> （存储层是 JSON 文本，与 `trigger_args` 同一套约定）；坏值丢弃而非抛错。

---

## ER 图

```
settings (key-value)

groups
  └── credentials (group_id FK)

plugins (credential_type ←→ credentials.credential_type)

mcp_permissions (tool_name)          ← 全局工具开关
mcp_tokens (tools_allowed JSON)      ← Token 级范围，与 mcp_permissions 取交集（无配置入口，恒为空）

schedules (target_id → groups.id 或 NULL；credential_ids → 勾选的凭据 id 列表)
```

---

## 迁移策略

`settings.schema_version` 记录当前版本（当前为 **6**）。应用启动时按版本号顺序执行迁移：

- **v1 → v2**：`credentials` 新增 `last_valid_at`；`plugins` 新增 `category` / `category_name`；
  凭据类型从整包命名（`llm_api_key` / `api_service`）迁移为子插件命名
  （`llm_<vendor>` / `api_<service>`）；回填 `last_valid_at`；删除 `verification_history` 表；
  移除 `vault_install_plugin` 权限记录。
- **v2 → v3**：`plugins` 新增 `batch_verify`（默认 1）；`settings` 新增 `verify_proxy`（验证代理）。
- **v3 → v4**：插件不再区分内置/外置，移除 `plugins.is_builtin`。
- **v4 → v5**：代理 id 由自增整数改为 UUID。
- **v5 → v6**：`schedules` 新增 `credential_ids`（默认 `'[]'`）—— 定时计划可只验证**勾选的具体凭据**
  （`target_scope='credentials'`）。老库按 `PRAGMA table_info` 判存在后补列，已有计划读作空数组。

> **`plugins.enabled` 的语义变更**：它已由「功能闸门」**降级为只读镜像**——插件的启用/禁用改由
> `plugins/.state.json` 决定，`sync_plugins()` 在每次扫描时把该状态文件镜像进这一列；
> `get_plugin_by_type()` **不按 `enabled` 过滤**。因此**禁用插件不影响其凭据的读写与备份导入**。
