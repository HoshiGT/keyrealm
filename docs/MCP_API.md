# MCP 接口规范

## 连接信息

| 项目 | 值 |
|------|----|
| 传输协议 | SSE（Server-Sent Events） |
| 默认地址 | `http://localhost:8765/sse` |
| 随应用启动 | 否（在「MCP 设置」页手动启动；启动后窗口最小化到托盘仍持续运行） |
| 鉴权方式 | Bearer Token（必须） |
| 权限控制 | 全局工具开关（界面唯一入口）；鉴权层另保留 Token 级 `tools_allowed` 子集，当前无配置入口 |

---

## Token 鉴权

### 获取 Token

MCP 全局只保留**一个活跃 Token**，且**默认即存在**（无需手动生成）：应用在读取 Token 时若无
活跃 Token 会自动创建一个。Token 形如：

```
cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7
```

在应用 **MCP 设置** 页可查看 Token 前缀；需要更换时点击「轮换 Token」，旧 Token 立即作废，
新 Token 明文**仅在轮换时展示一次**（数据库只保存 SHA-256 哈希与前缀），请立即复制到 Agent 配置中。

### 请求头格式

所有 SSE 连接必须在 HTTP Header 中携带 Token：

```
Authorization: Bearer cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7
```

### 鉴权失败响应

Bearer Token 由 SSE 中间件校验，失败时在建立连接前直接返回 **401** JSON：

```json
// 缺少 Authorization 头
{"error": "missing_token", "message": "需要 Bearer Token"}

// Token 无效或已被吊销
{"error": "invalid_token", "message": "Token 无效或已撤销"}

// Token 已过期（expires_at 早于当前时间）
{"error": "token_expired", "message": "Token 已过期"}
```

> **权限不足不是 HTTP 403**：它发生在工具调用内部。**返回的是可读的错误载荷**——刻意**不抛异常**，
> 因为客户端会把工具异常压成一句「Error executing tool xxx」、**把原因吞掉**，调用方无从得知
> 这两个工具默认关闭。正文形如：
>
> ```json
> {"error": "permission_denied", "tool": "vault_create_credential", "reason": "global_disabled",
>  "message": "工具「vault_create_credential」未授权：它在 MCP 里默认就是关闭的（高危工具）。请到
>              Credential Vault →「设置 → MCP 服务 → 工具权限」勾选启用后重试。"}
> ```
>
> `reason` 区分是哪一层拒绝：**`global_disabled`**（全局开关没开 → 去「设置 → MCP 服务 →
> 工具权限」勾选）或 **`token_scope`**（当前 Token 的允许范围不含它 → 为该 Token 勾选或换 Token）。
> 返回类型与各工具的声明**保持一致**（`dict` 工具返回该对象、列表工具返回 `[该对象]` 单元素数组），
> 以免触发 MCP 的输出校验。

### Token 管理（默认存在 · 仅轮换）

Token 的工具范围统一继承「工具权限」全局开关，**不在 Token 上单独配置**。需要更换 Token 时使用
「轮换」——轮换会立即作废旧 Token 并签发新 Token，新明文仅在轮换时展示一次（数据库只保存其
SHA-256 哈希与前缀）。

| 操作 | 说明 |
|------|------|
| 轮换 | 作废旧 Token 并签发新 Token，明文仅展示一次 |
| 查看 | 展示前缀（`cv_a3f2...`）与最近使用时间 |

---

## 客户端配置示例

### Claude Desktop

```json
{
  "mcpServers": {
    "credential-vault": {
      "url": "http://localhost:8765/sse",
      "headers": {
        "Authorization": "Bearer cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7"
      }
    }
  }
}
```

### Cursor

```json
{
  "mcpServers": {
    "credential-vault": {
      "url": "http://localhost:8765/sse",
      "headers": {
        "Authorization": "Bearer cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7"
      }
    }
  }
}
```

### Claude Code（CLI）

```bash
claude mcp add credential-vault \
  --transport sse \
  --url http://localhost:8765/sse \
  --header "Authorization: Bearer cv_a3f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7"
```

---

## 工具列表

### 凭据管理

#### `vault_list_credentials`
**默认权限：✅ 开启**

列出所有凭据（不返回敏感字段明文）。返回值为**裸数组**（不是带 `credentials` / `total` 的对象）。

```json
// 入参
{
  "group_id": 1,                    // 可选，按分组过滤
  "credential_type": "llm_openai",  // 可选，按类型过滤
  "status": "valid",                // 可选，按状态过滤
  "search": "生产"                   // 可选，按名称模糊搜索
}

// 出参（裸数组）
[
  {
    "id": 1,
    "name": "OpenAI 生产账号",
    "credential_type": "llm_openai",
    "group_name": "工作项目",          // 未分组时为 null
    "status": "valid",
    "data_plain": {"base_url": "https://api.openai.com/v1", "notes": "主账号"},
    "last_verified_at": "2026-10-03T10:30:00Z"
  }
]
```

---

#### `vault_get_credential`
**默认权限：❌ 关闭（需授权）**

获取单条凭据详情。**敏感字段既不返回明文、也不返回掩码**（要明文请用 `vault_get_secret`）。
凭据不存在时返回 `{"error": "not_found"}`。

```json
// 入参
{"credential_id": 1}

// 出参
{
  "id": 1,
  "name": "OpenAI 生产账号",
  "credential_type": "llm_openai",
  "group_id": 1,
  "group_name": "工作项目",
  "status": "valid",
  "status_detail": {"account": "user@example.com", "org": "MyOrg"},
  "data_plain": {"base_url": "https://api.openai.com/v1", "notes": "主账号"},
  "last_verified_at": "2026-10-03T10:30:00Z",
  "created_at": "2026-10-01T08:00:00Z",
  "updated_at": "2026-10-03T10:30:00Z"
}
```

---

#### `vault_get_secret`
**默认权限：❌ 关闭（高危，需显式授权）**

获取解密后的敏感字段明文。`field` 省略时返回该凭据的全部字段。

```json
// 入参：只要一个字段
{"credential_id": 1, "field": "api_key"}

// 出参
{"field": "api_key", "value": "sk-proj-xxxxxxxxxxxxxxxx"}

// 入参：省略 field（返回全部）
{"credential_id": 1}

// 出参
{"data": {"api_key": "sk-proj-xxxxxxxxxxxxxxxx", "base_url": "https://api.openai.com/v1"}}
```

> 凭据不存在返回 `{"error": "not_found"}`；解密失败返回
> `{"error": "decrypt_failed", "message": "..."}`；保险库锁定时返回 `{"error": "vault_locked"}`。

---

#### `vault_create_credential`
**默认权限：❌ 关闭（需授权）**

创建新凭据。与界面「添加凭据」共用**同一份实现**，因此同样会继承类别 / 插件的默认设置
（预填值、代理预留键等）。`auto_verify=true`（默认）时创建后立即验证并写回状态。

```json
// 入参
{
  "name": "OpenAI 测试账号",
  "credential_type": "llm_openai",
  "data": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-test-xxx",
    "notes": "测试环境"
  },
  "group_id": 1,          // 可选
  "auto_verify": true     // 可选，默认 true
}

// 出参：credential 为新建的凭据记录（字段同 vault_get_credential）
{"ok": true, "credential": {"id": 2, "name": "OpenAI 测试账号", "status": "valid", "...": "..."}}
```

> 参数校验失败或插件不存在时，直接返回该错误对象（形如 `{"error": "..."}`），不包 `ok`。

---

#### `vault_update_credential`
**默认权限：❌ 关闭（需授权）**

修改凭据。`name` / `group_id` / `data` 都会按传入值覆盖，未传的字段不动。

```json
// 入参
{
  "credential_id": 1,
  "name": "OpenAI 生产账号（已更新）",  // 可选
  "group_id": 2,                       // 可选
  "data": {"api_key": "sk-new-xxx"}    // 可选
}

// 出参：credential 为更新后的记录
{"ok": true, "credential": {"id": 1, "name": "OpenAI 生产账号（已更新）", "...": "..."}}
```

> **`data` 是整体替换**：其中**敏感字段**（manifest 里 `sensitive: true`）若缺失或为空，会用
> 库中已存明文回填，避免「只改备注却把密钥抹掉」；**非敏感字段**则以传入内容为准——没传的
> 非敏感字段会丢失，建议一并传入。凭据不存在返回 `{"error": "not_found"}`。

---

#### `vault_delete_credential`
**默认权限：❌ 关闭（需授权）**

```json
// 入参
{"credential_id": 1}

// 出参
{"ok": true}
```

---

### 验证

#### `vault_verify_credential`
**默认权限：✅ 开启**

对指定凭据触发验证，**同步**等待并返回结果（耗时由插件与超时设置决定）。返回内容即为该
凭据写入 `status_detail` 的那份数据。同一凭据正在验证中时直接返回
`{"status": "unknown", "message": "该凭据正在验证中，请稍候"}`。

```json
// 入参
{"credential_id": 1}

// 出参
{
  "status": "valid",                                               // valid / invalid / error / unknown
  "message": "API Key 有效",
  "account_info": {"account": "user@example.com", "org": "MyOrg"}, // 可能为 null
  "duration_ms": 420
}
```

---

#### `vault_batch_verify`
**默认权限：❌ 关闭（需授权）**

**异步**：立即返回、不等结果，进度通过 SSE 事件推送
（`vault:batch_verify_started` / `vault:batch_verify_progress` / `vault:batch_verify_done`）。

> 声明 `batch_verify: false` 的凭据类型（无法自动化验证或有写副作用，如 `sms_gateway`、
> `dns_duckdns`、`im_telegram_apihash`）会被自动跳过，不计入本次验证范围。

```json
// 入参
{
  "scope": "all",          // "all" | "group" | "type"
  "group_id": null,        // scope=group 时必填
  "credential_type": null  // scope=type 时必填
}

// 出参：仅表示已启动，不回传条数（总数以下发的进度事件为准）
{"ok": true, "message": "批量验证已启动"}
```

---

### 分组管理

#### `vault_list_groups`
**默认权限：✅ 开启**

```json
// 出参（裸数组，含分组全字段与 credential_count）
[
  {"id": 1, "name": "工作项目", "description": "", "color": "#409EFF",
   "sort_order": 0, "created_at": "2026-10-01T08:00:00Z", "credential_count": 5},
  {"id": 2, "name": "个人", "description": "", "color": "#67C23A",
   "sort_order": 0, "created_at": "2026-10-01T08:00:00Z", "credential_count": 3}
]
```

---

#### `vault_create_group`
**默认权限：❌ 关闭（需授权）**

```json
// 入参
{"name": "新项目", "description": "2026年Q4项目", "color": "#E6A23C"}

// 出参：group 为新建的分组记录
{"ok": true, "group": {"id": 3, "name": "新项目", "description": "2026年Q4项目", "color": "#E6A23C", "...": "..."}}
```

---

### 插件查询

#### `vault_list_plugins`
**默认权限：✅ 开启**

列出当前扫描到的全部插件（`plugins/` 目录扫描结果，放入即加载、删除即卸载，无安装接口）。
**被禁用的插件也在列表里**，看 `enabled` 字段区分（`plugins/.state.json` 决定；禁用只关功能闸门，
其凭据仍可读写）。

```json
// 出参（裸数组；下列为节选，实际是 plugins 表整行 + 若干附加字段）
[
  {
    "id": 3,
    "name": "OpenAI",
    "version": "1.0.0",
    "author": "Credential Vault",
    "description": "验证 OpenAI API Key 有效性",
    "credential_type": "llm_openai",
    "icon": "logo.png",
    "category": "llm",
    "category_name": "LLM 模型",
    "plugin_path": "plugins/llm/openai",
    "enabled": 1,               // 0/1 整数（1=启用，0=禁用），不是布尔
    "batch_verify": 1,          // 0/1 整数（0=不参与批量验证）
    "installed_at": "2026-10-01T08:00:00Z",
    "fields_schema": [{"key": "api_key", "label": "API Key", "sensitive": true, "...": "..."}],
    "credential_count": 8,      // 附加字段
    "category_icon": "🤖",       // 附加字段
    "rel_path": "llm/openai",   // 附加字段
    "icon_image": "data:...",   // 附加字段：icon 是图片文件名时读成 data URL，否则 null
    "icon_lift": false          // 附加字段：深色主题下该 logo 要不要垫浅色底（判据见 backend/icon_ink.py）
  }
]
```

> 调用时会先做一次插件目录扫描（`sync_plugins()`），使新放入 `plugins/` 的插件即时可见。

---

## 备份

> 与界面上的「备份」页共用同一套实现（`backend/backup.py` + `api.build_backup_doc` /
> `api.apply_backup_doc`），区别是 **MCP 没有文件对话框**：导出要么把文档直接返回，要么
> 明确给一个 `path` 落盘；导入要么给文档对象，要么给文件路径。两个工具都**默认关闭**。

#### `vault_export_backup`
**默认权限：❌ 关闭（高危）**

导出备份文档。触及整库数据，因此与 `vault_get_secret` 同级管控。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `scope` | `"all"` / `"credentials"` | `"all"` | `all` = 凭据 + 分组 + 偏好设置；`credentials` = 仅凭据 |
| `encrypted` | bool | `true` | `true` 敏感字段保持密文（需**同一主密码**导入）；**`false` 会把密钥明文写出，危险** |
| `credential_ids` | list[int] | 省略 | 只导出这些凭据 id；省略 = 全部 |
| `path` | str | 省略 | 写入该文件；**省略则把文档放在返回值的 `document` 字段里** |

```json
// 省略 path：文档直接返回，便于转手交给 vault_import_backup 或由 Agent 自行保存
{ "ok": true, "count": 12, "scope": "all", "encrypted": true, "document": { "format": "credential-vault-backup", "...": "..." } }

// 给了 path：落盘并回显路径
{ "ok": true, "count": 12, "scope": "all", "encrypted": true, "path": "D:/bak/vault-20261005.json" }
```

#### `vault_import_backup`
**默认权限：❌ 关闭（高危）**

把备份并入当前库（合并语义）。`document` 与 `path` **二选一**。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `document` | object | — | 备份 JSON 对象（可直接用 `vault_export_backup` 的返回值） |
| `path` | str | — | 备份文件路径（服务端所在机器的路径） |
| `dedupe` | bool | `true` | 「同类型 + 敏感值相同」的凭据跳过 |

```json
// 出参
{ "ok": true, "encrypted": true, "imported": 9, "skipped_plugin": 0, "skipped_duplicate": 3, "created_groups": 1, "source": "document" }
```

加密备份需与当前主密码一致，否则返回 `{"ok": false, "message": "备份是加密的，且与当前主密码不匹配，无法导入"}`。

---

## 权限管理

### 双层权限模型

```
工具调用请求
    │
    ▼ 第一层：mcp_permissions 表（全局工具开关）
    │   enabled=0 → 拒绝，无论 Token 如何配置
    │
    ▼ 第二层：Token.tools_allowed（Token 级范围）
    │   空数组 [] = 继承全局；非空 = 只允许列表内工具
    │   鉴权层保留该能力，但界面与 API 均无配置入口，现网恒为空 = 继承全局
    │
    ▼ 两层均通过 → 执行工具
```

### 权限粒度

| 权限级别 | 工具 | 默认 |
|----------|------|------|
| 只读 | `vault_list_credentials`, `vault_verify_credential`, `vault_list_groups`, `vault_list_plugins` | ✅ 开启 |
| 写操作 | `vault_get_credential`, `vault_create_*`, `vault_update_*`, `vault_delete_*`, `vault_batch_verify` | ❌ 关闭 |
| 高危 | `vault_get_secret`, `vault_export_backup`, `vault_import_backup` | ❌ 关闭 |

> **二次确认只在界面**：在 MCP 设置页**开启**高危 / 写操作工具时会弹确认框（清单见
> `docs/FRONTEND.md`）。它约束的是「用户开不开这个开关」，与 Agent 的调用无关——Agent 调用
> 一个已开启的工具时**不会**再弹任何确认。

### 未授权调用响应

两层权限对应两种**不同形态**的失败，不要混淆：

- **Token 层（HTTP 401）**：连接建立前即被 SSE 中间件拒绝，返回 JSON，见上文
  「鉴权失败响应」的 `missing_token` / `invalid_token` / `token_expired`。
- **工具权限层**：`_require_perm` 在工具内部抛 `PermissionError`，客户端收到的是一次
  `isError` 的**工具结果**（非 401/403），正文形如：

```
权限不足：vault_create_credential 未授权
```

> 全局开关关闭与 Token 范围不匹配都走这条路径，错误文本相同，不区分是哪一层拒绝的。

---

## MCP Server 启动/停止

MCP Server **不随应用自动启动**，需在 **MCP 设置** 页点击「启动服务」手动开启；开启后窗口最小化到托盘仍**持续运行**，Agent 可随时连接。

监听端口可在 **MCP 设置** 中修改（默认 8765）。若端口被占用，**直接报错、不自动换端口**——端口是用户显式配置的，静默改用 8766/8767 会造成「配了 8765 却跑在别的端口」的困惑。

停止 MCP Server：**MCP 设置** 页点击「停止服务」；**真正退出应用**（连同 MCP Server 一起停止）通过托盘右键菜单"退出"或关闭确认框的"直接退出"。
