# 架构设计文档

## 1. 系统总览

```
┌──────────────────────────────────────────────────────────────┐
│                         系统托盘                              │
│  [图标] 右键菜单：显示窗口 / MCP状态 / 退出                   │
│  关闭窗口 → 最小化到托盘（后台持续运行）                      │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                        桌面应用窗口                           │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               Vue 3 前端（WebView）                     │  │
│  │  凭据管理 │ 分组 │ MCP │ 设置                          │  │
│  └───────────────────────┬────────────────────────────────┘  │
│                          │ window.pywebview.api.*             │
│  ┌───────────────────────▼────────────────────────────────┐  │
│  │               Python 后端（pywebview JS-API）           │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────────────────────┐  │  │
│  │  │ Crypto  │ │ Database │ │    Plugin Manager       │  │  │
│  │  │ AES-GCM │ │  SQLite  │ │  目录扫描，放入即加载   │  │  │
│  │  └─────────┘ └──────────┘ └─────────────────────────┘  │  │
│  │  ┌─────────────────┐  ┌────────────────────────────┐   │  │
│  │  │    Scheduler    │  │       MCP Server           │   │  │
│  │  │  APScheduler    │  │  FastMCP (SSE:8765)        │   │  │
│  │  │                 │  │  + Bearer Token 鉴权       │   │  │
│  │  └─────────────────┘  └────────────────────────────┘   │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │              System Tray（pystray）               │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────────┐          ┌─────────────────┐
  │  ./data/        │          │  外部 API 服务   │
  │  credentials.db │          │ OpenAI/Anthropic │
  │  (项目目录内)   │          │ 豆包/通义/文心   │
  └─────────────────┘          └─────────────────┘
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

**数据库位置**：项目目录内 `./data/credentials.db`，避免跨目录污染。

```
API-Key-Checker/
└── data/                  # 已在 .gitignore 中排除
    ├── credentials.db
    ├── credentials.db-wal
```

> 应用启动时自动创建 `data/` 目录。数据库路径相对于 `main.py` 所在目录解析，与操作系统无关。

**连接策略**：单连接 + 线程锁（SQLite WAL 模式），避免并发写冲突。

---

### 2.3 插件管理器（plugin_manager.py）

**职责**：扫描 `plugins/` 目录、注册插件、按需加载插件模块。**放入即加载，删除目录即卸载**。

**插件删除 / 添加（插件管理）**：`delete_plugin(credential_type)` 删除该插件的 `plugins/` 目录、卸载模块、删除插件记录，并（默认）删除该类型下全部凭据；`list_available_plugins()` / `install_plugin()` 从内置纯净源 `plugins_builtin/`（发行包随 exe 同级提供）把插件复制回 `plugins/` 并加载。目录删除有路径越界保护。

**插件目录**：`<app>/plugins/` 是唯一事实来源，采用两层结构：

```
plugins/
├── llm/                      # 类别目录
│   ├── category.json         # 类别显示名 / 图标
│   ├── llm_common.py         # 类别共享模块（底层通用工具 + 子插件原型接口，不含厂商逻辑）
│   ├── llm_protocols.py      # 协议级工具（探针 / 简单对话），供子插件按需复用
│   ├── openai/               # 服务子插件（独立，可单独删除）
│   │   ├── manifest.json
│   │   └── plugin.py
│   ├── deepseek/
│   └── …                     # 共 18 个 LLM 子插件
├── api/                      # API 服务类别（firecrawl / tavily / coze，共 3 个）
├── dns/                      # 域名解析：dnspod / duckdns / godaddy
├── dev/                      # 开发者平台：github / huggingface / wandb
├── im/                       # 即时通讯：feishu / dingtalk / telegram / discord / qq_bot / wechat_ilink
├── cloud/                    # 云服务商：aliyun / tencentcloud / cloudflare
├── exchange/                 # 交易所：ht / okx / binance
└── mail/                     # 邮箱：smtp（通用 SMTP，覆盖 Gmail/QQ/163/126）
```

每个类别目录结构一致：`category.json` + 可选 `<类别>_common.py` + 各服务子目录（`manifest.json` + `plugin.py`）。

- **一个服务 = 一个独立子插件**：各自持有 `manifest.json`（字段 schema、credential_type）与薄 `plugin.py`；删除任一子插件目录不影响其他子插件运行。根目录下直接含 `manifest.json` 的目录视为无类别的平铺插件。
- **类别共享模块**：`<类别>_common.py` 只放**底层通用工具 + 子插件原型接口**（`VerifyResult` / `make_client` / `safe_json` / `guarded` 等）；**协议级工具**（如 `plugins/llm/llm_protocols.py` 的探针与 `chat_completion`）另建独立工具模块供子插件按需引用。加载子插件时临时将其目录与类别目录注入 `sys.path`（见 plugin_base.load_plugin_module）。**服务专属逻辑在各子插件内自带，新增插件无需改动共享模块。**
- **同步流程**：启动时 `sync_plugins()` 扫描两层结构 → upsert plugins 表（含 category / category_name / batch_verify）→ 清理已消失插件的记录与模块缓存。
- **批量验证开关**：manifest 的 `batch_verify`（默认 true）落库到 plugins.batch_verify；调度器与前端批量时跳过为 false 的类型（无法自动化或有写副作用，如 duckdns / wechat_ilink / ht）——前端在批量勾选弹窗中禁用其勾选。

**插件模块约定**：必须暴露 `verify(data)`；可选 `validate(data)`、`get_display_summary(data)`。`verify` 返回值鸭子类型兼容 VerifyResult / dataclass / dict。

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
    │   （自动剔除 batch_verify=false 的类型，计为「跳过」）
    │
    ▼ 并发调用各插件的 verify()（每条凭据一个任务，BATCH_CONCURRENCY=8 并发）
    │
    ▼ 更新 credentials.status / last_verified_at
    │   （status == valid 时同时更新 last_valid_at）
    │
    ▼ 通过 pywebview 事件通知前端刷新
```

**验证代理**：插件发起校验请求时的代理由 `backend/proxy.py` 统一管理——它把「系统设置」中的代理写入环境变量 `VAULT_PROXY`，各 `<类别>_common.py` 的 `make_client()` 读取该变量（外部同名环境变量优先）。见 `docs/PLUGIN_GUIDE.md` 第 2.1 节。

**备份 / 恢复**：`backend/backup.py` 负责构建/导入备份文档（JSON）。加密备份的敏感字段直接沿用库中密文，只能用同一主密码导入；明文备份可跨主密码导入（以当前主密钥重新加密写库）。导入按「类型 + 敏感值」去重，并跳过插件未安装的条目；文件读写通过 pywebview 的原生文件对话框（`api._file_dialog`）。

---

### 2.5 MCP Server（mcp_server.py）

**职责**：为 AI Agent 提供凭据管理接口。

**传输**：SSE（Server-Sent Events），默认端口 `8765`。不随应用自动启动，由用户在「MCP 设置」页手动开启；开启后窗口最小化到托盘仍持续运行。

**Token 鉴权**（必须）：
```
Agent 发起 SSE 连接
    │
    ▼ HTTP Header: Authorization: Bearer cv_<32位hex>
    │
    ▼ [MCP Server] 计算 SHA-256(token)，查询 mcp_tokens 表
    │
    ├── 未找到 / 已过期 / 已吊销 → 401 Unauthorized，拒绝连接
    │
    └── 找到有效 token
            │
            ▼ 检查该 token 的 tools_allowed 范围
            │
            ▼ 每次工具调用前：检查全局 mcp_permissions 表 + token 范围
            │
            ▼ 更新 last_used_at
```

**Token 格式**：`cv_` + 32 位随机十六进制（共 35 字符），数据库中只存 SHA-256 哈希，明文仅在创建 / 轮换时展示一次。

MCP 全局只保留**一个活跃 Token**，且**默认即存在**（无则自动创建），仅提供「轮换」
（作废旧 Token 并签发新 Token，明文仅展示一次）。

**权限控制**：
1. **全局工具开关**（`mcp_permissions` 表）：在 MCP 设置页统一控制，是唯一的权限配置入口
2. **Token 级范围**（`mcp_tokens.tools_allowed`）：鉴权层仍支持按 Token 限定工具子集，
   但界面不再暴露该配置——默认空值表示继承全局开关

调用需同时通过全局开关与 Token 范围（当前默认继承全局，等价于只受全局开关约束），
任一层拒绝则返回 `permission_denied`。

详见 [MCP_API.md](MCP_API.md)。

---

### 2.6 pywebview API 桥接层（api.py）

**职责**：暴露给前端的 Python 方法集合，是前后端唯一通信通道。

**调用方式**（前端）：
```javascript
// 所有方法均为 async
const result = await window.pywebview.api.list_credentials({ group_id: 1 })
```

**方法命名规范**：`动词_名词`，如 `create_credential`、`verify_credential`、`check_duplicate_credential`。

**事件推送**（后端 → 前端）：
```python
# 后端发送事件
window.evaluate_js("window.dispatchEvent(new CustomEvent('vault:updated', {detail: ...}))")
```

---

### 2.7 系统托盘（tray.py）

**职责**：窗口最小化到托盘后保持后台运行，MCP Server 和调度器持续工作。

**实现**：`pystray` 库（跨平台，Windows/macOS/Linux 均支持）。

**托盘图标状态**：

| 图标颜色 | 含义 |
|----------|------|
| 🟢 绿色 | MCP Server 运行中，至少一个 Token 活跃 |
| 🔵 蓝色 | MCP Server 运行中，无活跃连接 |
| 🔴 红色 | MCP Server 已停止或出错 |
| ⚪ 灰色 | 应用未解锁（主密码未输入） |

**右键菜单**：
```
┌─────────────────────────┐
│  显示主窗口              │
│  ─────────────────────  │
│  MCP：运行中 :8765      │
│  活跃 Token：1 个       │
│  ─────────────────────  │
│  锁定（清除内存密钥）    │
│  ─────────────────────  │
│  退出                   │
└─────────────────────────┘
```

**窗口关闭行为**（由设置项 `close_action` 决定）：
- `ask`（默认）→ 弹窗让用户选择"直接退出"或"后台运行"，可勾选"记住我的选择"
- `quit` → 停止所有后台服务，真正退出进程
- `background` → 隐藏窗口，最小化到托盘（不退出）
- 托盘菜单"退出" → 无论设置如何，均停止所有后台服务并退出
- 首次最小化时弹出气泡提示："Credential Vault 已最小化到托盘，MCP 服务持续运行"

**托盘线程**：`pystray` 在独立守护线程中运行，通过线程安全队列与主线程通信。

---

### 2.8 日志模块（logger.py）

**职责**：统一日志配置，写入 `<app_dir>/data/logs/credential_vault.log`，并可按用户选择调整级别。

**实现**：`logging` + `RotatingFileHandler`（单文件约 1 MB × 5 份备份），多行堆栈按时间戳归组。

**日志级别**：`DEBUG / INFO / WARNING / ERROR / CRITICAL`（默认 `INFO`），运行时可调整并持久化到
`settings` 表的 `log_level` 键，下次启动自动恢复。`httpx` / `httpcore` 被压制到 `WARNING`，
避免第三方库噪声。

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
    ▼ [后端] 更新 status / last_verified_at（valid 时同时更新 last_valid_at）
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
    │       └── 窗口关闭事件 → 拦截，改为隐藏窗口
    │
    ├── System Tray（pystray 守护线程）
    │       └── 托盘菜单点击 → 通过线程队列通知主线程
    │
    ├── BackgroundScheduler（APScheduler 线程池）
    │       └── 定时验证任务（ThreadPoolExecutor，max_workers=10）
    │
    └── MCP Server（uvicorn 线程，端口 8765）
            ├── SSE 连接处理（asyncio 事件循环）
            └── Token 鉴权中间件（每次请求校验 Bearer Token）
```

> **注意**：
> - pywebview 的 JS-API 调用在独立线程中执行，SQLite 操作需加线程锁
> - 窗口隐藏后所有后台线程继续运行，MCP Server 不中断
> - 应用真正退出时（托盘菜单"退出"）：停止调度器 → 关闭 MCP Server → 销毁托盘图标 → 退出进程

---

## 5. 安全设计

| 威胁 | 缓解措施 |
|------|----------|
| 数据库文件泄露 | AES-256-GCM 字段级加密，无主密码无法解密 |
| 主密码暴力破解 | PBKDF2 100,000 次迭代，增加破解成本 |
| 内存中密钥泄露 | 密钥仅存于进程内存，不写文件；托盘"锁定"可主动清除 |
| MCP 未授权访问 | Bearer Token 鉴权，Token 哈希存储，支持吊销和过期时间 |
| MCP Token 泄露 | 每个 Token 可单独限定工具范围；吊销后立即失效；记录 last_used_at 便于审计 |
| MCP 工具越权 | 双层权限：全局工具开关 + Token 级 tools_allowed，取交集 |
| 插件恶意代码 | 插件在独立命名空间加载，文档中明确信任边界 |
| 验证请求泄露 Key | 验证逻辑由插件实现，Key 仅发送给对应厂商 API |
| 后台运行时窗口未锁 | 托盘菜单提供"锁定"操作，清除内存密钥，需重新输入主密码 |

---

## 6. 扩展性设计

- **新凭据类型**：在 `plugins/` 下新增子插件目录（manifest.json + plugin.py），重启即自动加载，无需修改主程序
- **新 MCP 工具**：在 `mcp_server.py` 中注册，自动出现在权限管理页
- **新验证厂商**：新建 `plugins/llm/<厂商>/`（`manifest.json` + `plugin.py`），照抄同级子插件、自带端点与判定，**无需修改 `llm_common.py`**；重启即加载
- **数据库迁移**：`database.py` 中维护 `SCHEMA_VERSION`，启动时自动执行迁移脚本
