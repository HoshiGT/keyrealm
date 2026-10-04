# MCP 接口规范

## 连接信息

| 项目 | 值 |
|------|----|
| 传输协议 | SSE（Server-Sent Events） |
| 默认地址 | `http://localhost:8765/sse` |
| 随应用启动 | 否（在「MCP 设置」页手动启动；启动后窗口最小化到托盘仍持续运行） |
| 鉴权方式 | Bearer Token（必须） |
| 权限控制 | 双层：全局工具开关 + Token 级 tools_allowed |

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

```json
// 401 Unauthorized — Token 无效、已吊销或已过期
{
  "error": "unauthorized",
  "message": "Invalid or expired token"
}

// 403 Forbidden — Token 有效但无权调用该工具
{
  "error": "permission_denied",
  "message": "Token does not have access to vault_create_credential"
}
```

### Token 管理（默认存在 · 仅轮换）

MCP 全局只保留**一个活跃 Token**，且**默认即存在**（无需手动生成）：应用在读取 Token 时若无
活跃 Token 会自动创建一个。Token 的工具范围统一继承「工具权限」全局开关，**不在 Token 上单独
配置**。需要更换 Token 时使用「轮换」——轮换会立即作废旧 Token 并签发新 Token，新明文仅在
轮换时展示一次（数据库只保存其 SHA-256 哈希与前缀）。

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

列出所有凭据（不返回敏感字段明文）。

```json
// 入参
{
  "group_id": 1,           // 可选，按分组过滤
  "credential_type": "llm_openai",  // 可选，按类型过滤
  "status": "valid"        // 可选，按状态过滤
}

// 出参
{
  "credentials": [
    {
      "id": 1,
      "name": "OpenAI 生产账号",
      "credential_type": "llm_openai",
      "group_id": 1,
      "group_name": "工作项目",
      "status": "valid",
      "last_verified_at": "2026-10-03T10:30:00Z",
      "last_valid_at": "2026-10-03T10:30:00Z",
      "data_plain": {"base_url": "https://api.openai.com/v1", "notes": "主账号"},
      "data_masked": {"api_key": "sk-pro…abcd"},
      "created_at": "2026-10-01T08:00:00Z"
    }
  ],
  "total": 1
}
```

---

#### `vault_get_credential`
**默认权限：❌ 关闭（需授权）**

获取单条凭据详情，敏感字段以掩码形式返回（如 `sk-****...****`）。

```json
// 入参
{"id": 1}

// 出参
{
  "id": 1,
  "name": "OpenAI 生产账号",
  "credential_type": "llm_openai",
  "data_plain": {"base_url": "https://api.openai.com/v1", "notes": "主账号"},
  "data_masked": {"api_key": "sk-pro…abcd"},
  "status": "valid",
  "status_detail": {"account": "user@example.com", "org": "MyOrg"},
  "last_verified_at": "2026-10-03T10:30:00Z"
}
```

---

#### `vault_get_secret`
**默认权限：❌ 关闭（高危，需显式授权）**

获取解密后的敏感字段明文。Agent 调用此工具时，应用会弹出确认对话框。

```json
// 入参
{"id": 1, "field": "api_key"}

// 出参
{"value": "sk-proj-xxxxxxxxxxxxxxxx"}
```

---

#### `vault_create_credential`
**默认权限：❌ 关闭（需授权）**

创建新凭据，自动触发验证。

```json
// 入参
{
  "name": "OpenAI 测试账号",
  "credential_type": "llm_openai",
  "group_id": 1,
  "data": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-test-xxx",
    "notes": "测试环境"
  }
}

// 出参
{
  "id": 2,
  "status": "valid",
  "message": "凭据创建成功，验证通过"
}
```

---

#### `vault_update_credential`
**默认权限：❌ 关闭（需授权）**

修改凭据，仅传入需要修改的字段。

```json
// 入参
{
  "id": 1,
  "name": "OpenAI 生产账号（已更新）",  // 可选
  "group_id": 2,                          // 可选
  "data": {"api_key": "sk-new-xxx"}       // 可选，仅更新指定字段
}

// 出参
{"id": 1, "message": "更新成功", "reverify": true}
```

---

#### `vault_delete_credential`
**默认权限：❌ 关闭（需授权）**

```json
// 入参
{"id": 1}

// 出参
{"message": "删除成功"}
```

---

### 验证

#### `vault_verify_credential`
**默认权限：✅ 开启**

对指定凭据触发验证，同步等待结果（最长 30 秒）。

```json
// 入参
{"id": 1}

// 出参
{
  "id": 1,
  "status": "valid",
  "message": "API Key 有效",
  "account_info": {"account": "user@example.com", "org": "MyOrg"},
  "duration_ms": 420,
  "verified_at": "2026-10-03T10:35:00Z"
}
```

---

#### `vault_batch_verify`
**默认权限：❌ 关闭（需授权）**

批量验证，异步执行，立即返回任务 ID。

> 声明 `batch_verify: false` 的凭据类型（无法自动化验证或有写副作用，如 `dns_duckdns`、
> `im_wechat_ilink`、`exchange_ht`）会被自动跳过，不计入本次验证范围。

```json
// 入参
{
  "scope": "all",          // "all" | "group" | "type"
  "group_id": null,        // scope=group 时必填
  "credential_type": null  // scope=type 时必填
}

// 出参
{
  "task_id": "verify_20261003_103500",
  "total": 12,
  "message": "批量验证已启动，共 12 条凭据"
}
```

---

### 分组管理

#### `vault_list_groups`
**默认权限：✅ 开启**

```json
// 出参
{
  "groups": [
    {"id": 1, "name": "工作项目", "description": "", "color": "#409EFF", "credential_count": 5},
    {"id": 2, "name": "个人", "description": "", "color": "#67C23A", "credential_count": 3}
  ]
}
```

---

#### `vault_create_group`
**默认权限：❌ 关闭（需授权）**

```json
// 入参
{"name": "新项目", "description": "2026年Q4项目", "color": "#E6A23C"}

// 出参
{"id": 3, "message": "分组创建成功"}
```

---

### 插件查询

#### `vault_list_plugins`
**默认权限：✅ 开启**

列出当前已加载的全部插件（`plugins/` 目录扫描结果，放入即加载、删除即卸载，无安装接口）。

```json
// 出参
{
  "plugins": [
    {
      "name": "OpenAI",
      "version": "1.0.0",
      "credential_type": "llm_openai",
      "author": "Credential Vault",
      "description": "验证 OpenAI API Key 有效性",
      "icon": "🤖",
      "category": "llm",
      "category_name": "LLM 模型",
      "enabled": true,
      "credential_count": 8
    }
  ]
}
```

---

## 权限管理

### 双层权限模型

```
工具调用请求
    │
    ▼ 第一层：Token.tools_allowed（Token 级范围）
    │   空数组 [] = 继承全局；非空 = 只允许列表内工具
    │
    ▼ 第二层：mcp_permissions 表（全局工具开关）
    │   enabled=0 → 拒绝，无论 Token 如何配置
    │
    ▼ 两层均通过 → 执行工具
```

### 权限粒度

| 权限级别 | 工具 | 默认 |
|----------|------|------|
| 只读 | `vault_list_credentials`, `vault_verify_credential`, `vault_list_groups`, `vault_list_plugins` | ✅ 开启 |
| 写操作 | `vault_get_credential`, `vault_create_*`, `vault_update_*`, `vault_delete_*`, `vault_batch_verify` | ❌ 关闭 |
| 高危 | `vault_get_secret` | ❌ 关闭（需二次确认） |

### 未授权调用响应

```json
// Token 无效
{"error": "unauthorized", "message": "Invalid or expired token"}

// 全局工具未开启
{"error": "permission_denied", "message": "工具 vault_create_credential 全局未启用，请在 MCP 设置中开启"}

// Token 范围不含该工具
{"error": "permission_denied", "message": "当前 Token 无权调用 vault_create_credential"}
```

---

## MCP Server 启动/停止

MCP Server **不随应用自动启动**，需在 **MCP 设置** 页点击「启动服务」手动开启；开启后窗口最小化到托盘仍**持续运行**，Agent 可随时连接。

监听端口可在 **MCP 设置** 中修改（默认 8765）。若端口被占用，启动时自动尝试 8766、8767，并在界面和托盘提示实际端口。

停止 MCP Server：**MCP 设置** 页点击「停止服务」；**真正退出应用**（连同 MCP Server 一起停止）通过托盘右键菜单"退出"或关闭确认框的"直接退出"。
