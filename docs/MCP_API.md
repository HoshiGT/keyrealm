# MCP 接口规范

## 连接信息

| 项目 | 值 |
|------|----|
| 传输协议 | SSE（Server-Sent Events） |
| 默认地址 | `http://localhost:8765/sse` |
| 随应用启动 | 是 |
| 权限控制 | 按工具粒度，默认只读工具开启，写操作需手动授权 |

### Claude Desktop 配置示例

```json
{
  "mcpServers": {
    "credential-vault": {
      "url": "http://localhost:8765/sse"
    }
  }
}
```

### Cursor 配置示例

```json
{
  "mcpServers": {
    "credential-vault": {
      "url": "http://localhost:8765/sse"
    }
  }
}
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
  "credential_type": "llm_api_key",  // 可选，按类型过滤
  "status": "valid"        // 可选，按状态过滤
}

// 出参
{
  "credentials": [
    {
      "id": 1,
      "name": "OpenAI 生产账号",
      "credential_type": "llm_api_key",
      "group_id": 1,
      "group_name": "工作项目",
      "status": "valid",
      "last_verified_at": "2026-10-03T10:30:00Z",
      "data_plain": {"vendor": "openai", "notes": "主账号"},
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
  "credential_type": "llm_api_key",
  "data_plain": {"vendor": "openai", "notes": "主账号"},
  "data_masked": {"api_key": "sk-****abcd", "base_url": "https://api.openai.com/v1"},
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
  "credential_type": "llm_api_key",
  "group_id": 1,
  "data": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-test-xxx",
    "vendor": "openai",
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

### 插件管理

#### `vault_list_plugins`
**默认权限：✅ 开启**

```json
// 出参
{
  "plugins": [
    {
      "name": "LLM API Key",
      "version": "1.0.0",
      "credential_type": "llm_api_key",
      "author": "CredentialVault",
      "description": "管理和验证各大 LLM 厂商的 API Key",
      "is_builtin": true,
      "enabled": true,
      "credential_count": 8
    }
  ]
}
```

---

#### `vault_install_plugin`
**默认权限：❌ 关闭（需授权）**

从本地路径安装 `.zip` 插件。

```json
// 入参
{"zip_path": "/path/to/my_plugin.zip"}

// 出参
{"credential_type": "my_plugin", "name": "My Plugin", "message": "安装成功"}
```

---

## 权限管理

### 权限状态查询

前端 **MCP 设置** 页展示所有工具的开关状态，用户可逐一控制。

### 权限粒度

| 权限级别 | 工具 | 说明 |
|----------|------|------|
| 只读（默认开启） | `list_*`, `verify_credential` | 不暴露敏感数据，可安全开启 |
| 写操作（默认关闭） | `create_*`, `update_*`, `delete_*`, `batch_verify`, `install_plugin` | 修改数据，需用户信任 Agent 后开启 |
| 高危（默认关闭） | `vault_get_secret` | 返回明文密钥，开启前需二次确认 |

### 未授权调用响应

```json
{
  "error": "permission_denied",
  "message": "工具 vault_create_credential 未授权，请在应用 MCP 设置中开启"
}
```

---

## MCP Server 启动/停止

MCP Server 随主应用自动启动，监听端口可在 **设置 → MCP** 中修改（默认 8765）。

若端口被占用，应用启动时会自动尝试 8766、8767，并在界面提示实际端口。
