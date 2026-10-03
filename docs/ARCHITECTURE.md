# 架构设计文档

## 1. 系统总览

```
┌─────────────────────────────────────────────────────────┐
│                      桌面应用窗口                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │              Vue 3 前端（WebView）                  │  │
│  │  Dashboard │ 凭据管理 │ 分组 │ 插件 │ MCP │ 设置   │  │
│  └──────────────────────┬────────────────────────────┘  │
│                         │ window.pywebview.api.*         │
│  ┌──────────────────────▼────────────────────────────┐  │
│  │              Python 后端（pywebview JS-API）        │  │
│  │  ┌─────────┐ ┌──────────┐ ┌────────────────────┐  │  │
│  │  │ Crypto  │ │ Database │ │  Plugin Manager    │  │  │
│  │  │ AES-GCM │ │  SQLite  │ │  .zip 加载/热插拔  │  │  │
│  │  └─────────┘ └──────────┘ └────────────────────┘  │  │
│  │  ┌─────────────────┐  ┌────────────────────────┐  │  │
│  │  │   Scheduler     │  │     MCP Server         │  │  │
│  │  │  APScheduler    │  │  FastMCP (SSE:8765)    │  │  │
│  │  └─────────────────┘  └────────────────────────┘  │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────┐              ┌─────────────────┐
  │  SQLite DB  │              │  外部 API 服务   │
  │ (加密字段)  │              │ OpenAI/Anthropic │
  └─────────────┘              │ 豆包/通义/文心   │
                               └─────────────────┘
```

---

## 2. 核心模块

### 2.1 加密模块（crypto.py）

**职责**：主密码管理、AES 密钥派生、字段级加密/解密。

**密钥派生流程**：
```
用户主密码
    │
    ▼ PBKDF2-HMAC-SHA256（100,000 次迭代，随机 16 字节 salt）
    │
    ▼
32 字节 AES 密钥（仅存于内存，不落盘）
```

**加密方案**：AES-256-GCM（认证加密，防篡改）
- 每个字段独立加密，nonce 随机生成（12 字节）
- 存储格式：`base64(nonce + ciphertext + tag)`
- salt 存储在 `settings` 表，首次初始化时生成

**主密码验证**：
- 存储 `verify_token = AES-GCM(key, b"VAULT_OK")`
- 启动时用主密码派生密钥，尝试解密 verify_token
- 解密成功 → 密码正确；失败 → 密码错误

**主密码重置**：
- 用旧密码解密所有字段 → 用新密码重新加密 → 更新 verify_token 和 salt

---

### 2.2 数据库模块（database.py）

**职责**：SQLite 连接管理、CRUD 操作、数据迁移。

**数据库位置**：
- Windows：`%APPDATA%/CredentialVault/credentials.db`
- macOS：`~/Library/Application Support/CredentialVault/credentials.db`
- Linux：`~/.local/share/credential-vault/credentials.db`
- 使用 `platformdirs` 库自动选择

**连接策略**：单连接 + 线程锁（SQLite WAL 模式），避免并发写冲突。

---

### 2.3 插件管理器（plugin_manager.py）

**职责**：插件发现、安装、卸载、热加载。

**插件目录**：
- 内置插件：`<app>/plugins/`（随应用分发）
- 用户安装插件：`<data_dir>/installed_plugins/`

**安装流程**（.zip 拖入）：
```
.zip 文件
    │
    ▼ 校验 manifest.json 格式
    │
    ▼ 解压到 installed_plugins/<credential_type>/
    │
    ▼ 动态 import plugin.py（importlib）
    │
    ▼ 调用 plugin.register() 获取字段 schema
    │
    ▼ 写入 plugins 表
    │
    ▼ 通知前端刷新插件列表
```

**热卸载**：
- 从 `sys.modules` 移除模块引用
- 删除解压目录
- 从 plugins 表标记为 disabled（凭据数据保留）

---

### 2.4 调度器（scheduler.py）

**职责**：定时批量验证。

**实现**：APScheduler `BackgroundScheduler`，任务持久化到 SQLite `schedules` 表。

**调度类型**：
- `interval`：每隔 N 小时/天执行
- `cron`：cron 表达式（如每天 02:00）

**触发逻辑**：
```
调度器触发
    │
    ▼ 查询目标凭据（全部 / 指定分组 / 指定类型）
    │
    ▼ 并发调用各插件的 verify()（线程池，最大 10 并发）
    │
    ▼ 更新 credentials.status / last_verified_at
    │
    ▼ 写入 verification_history
    │
    ▼ 通过 pywebview 事件通知前端刷新
```

---

### 2.5 MCP Server（mcp_server.py）

**职责**：为 AI Agent 提供凭据管理接口。

**传输**：SSE（Server-Sent Events），默认端口 `8765`，随应用启动。

**权限控制**：
- `mcp_permissions` 表记录每个工具的启用状态
- 每次工具调用前检查权限，未授权则返回错误
- 前端 MCP 设置页可按工具粒度开关

详见 [MCP_API.md](MCP_API.md)。

---

### 2.6 pywebview API 桥接层（api.py）

**职责**：暴露给前端的 Python 方法集合，是前后端唯一通信通道。

**调用方式**（前端）：
```javascript
// 所有方法均为 async
const result = await window.pywebview.api.list_credentials({ group_id: 1 })
```

**方法命名规范**：`动词_名词`，如 `create_credential`、`verify_credential`、`install_plugin`。

**事件推送**（后端 → 前端）：
```python
# 后端发送事件
window.evaluate_js("window.dispatchEvent(new CustomEvent('vault:updated', {detail: ...}))")
```

---

## 3. 数据流

### 3.1 添加凭据（自动验证）

```
用户填写表单
    │
    ▼ [前端] api.create_credential(data)
    │
    ▼ [后端] 校验字段（调用 plugin.validate_fields()）
    │
    ▼ [后端] crypto.encrypt(敏感字段) → 写入 SQLite
    │
    ▼ [后端] 异步调用 plugin.verify(plain_data)
    │
    ▼ [后端] 更新 status / last_verified_at / verification_history
    │
    ▼ [后端] 推送 vault:credential_updated 事件
    │
    ▼ [前端] 刷新列表，显示验证结果
```

### 3.2 MCP Agent 调用

```
AI Agent（Claude Desktop / Cursor）
    │
    ▼ SSE 连接 http://localhost:8765/sse
    │
    ▼ 调用工具 vault_list_credentials
    │
    ▼ [MCP Server] 检查 mcp_permissions 表
    │
    ▼ [MCP Server] 调用 database.list_credentials()
    │
    ▼ [MCP Server] 解密敏感字段（按权限决定是否返回明文）
    │
    ▼ 返回结果给 Agent
```

---

## 4. 线程模型

```
主线程
    │
    ├── pywebview GUI 事件循环（主线程）
    │
    ├── BackgroundScheduler（APScheduler 线程池）
    │       └── 定时验证任务（ThreadPoolExecutor，max_workers=10）
    │
    └── MCP Server（uvicorn 线程，端口 8765）
            └── SSE 连接处理（asyncio 事件循环）
```

> **注意**：pywebview 的 JS-API 调用在独立线程中执行，SQLite 操作需加线程锁。

---

## 5. 安全设计

| 威胁 | 缓解措施 |
|------|----------|
| 数据库文件泄露 | AES-256-GCM 字段级加密，无主密码无法解密 |
| 主密码暴力破解 | PBKDF2 100,000 次迭代，增加破解成本 |
| 内存中密钥泄露 | 密钥仅存于进程内存，不写文件，进程退出即消失 |
| MCP 未授权访问 | 按工具粒度权限开关，默认只读，写操作需手动开启 |
| 插件恶意代码 | 插件在独立命名空间加载，文档中明确信任边界 |
| 验证请求泄露 Key | 验证逻辑由插件实现，Key 仅发送给对应厂商 API |

---

## 6. 扩展性设计

- **新凭据类型**：编写 `.zip` 插件，无需修改主程序
- **新 MCP 工具**：在 `mcp_server.py` 中注册，自动出现在权限管理页
- **新验证厂商**：在 LLM 插件的 `plugin.py` 中添加厂商适配类
- **数据库迁移**：`database.py` 中维护 `SCHEMA_VERSION`，启动时自动执行迁移脚本
