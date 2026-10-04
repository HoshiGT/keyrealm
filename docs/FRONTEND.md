# 前端设计文档

## 技术选型

| 技术 | 版本 | 用途 |
|------|------|------|
| Vue 3 | ^3.4 | 前端框架（Composition API） |
| Element Plus | ^2.7 | UI 组件库 |
| Pinia | ^2.1 | 状态管理 |
| Vue Router | ^4.3 | 页面路由 |
| Vite | ^5.2 | 构建工具 |
| @vitejs/plugin-vue | ^5.0 | Vue SFC 支持 |

## 与 Python 后端通信

pywebview 将 Python 对象注入为 `window.pywebview.api`，所有方法均为 `async`：

```javascript
// 调用后端方法
const result = await window.pywebview.api.list_credentials({ group_id: 1 })

// 监听后端推送事件
window.addEventListener('vault:credential_updated', (e) => {
  store.refreshCredential(e.detail.id)
})
```

> **开发模式**：Vite dev server（:5173）+ pywebview 连接 localhost  
> **生产模式**：`npm run build` → `frontend/dist/` → pywebview 加载本地文件

---

## 页面结构

```
App.vue
├── 侧边栏（Sidebar）
│   ├── 凭据管理（Credentials）
│   ├── 分组管理（Groups）
│   ├── MCP 设置（MCP）
│   └── 系统设置（Settings）
└── 主内容区（router-view）
```

---

## 页面详细设计

### 1. 首次启动 — 设置主密码

全屏遮罩，无侧边栏。

```
┌─────────────────────────────────┐
│         🔐 Credential Vault     │
│                                 │
│   设置主密码（首次使用）          │
│   ┌─────────────────────────┐   │
│   │  主密码                  │   │
│   └─────────────────────────┘   │
│   ┌─────────────────────────┐   │
│   │  确认主密码              │   │
│   └─────────────────────────┘   │
│   密码强度：████████░░  强       │
│                                 │
│        [ 创建保险库 ]           │
└─────────────────────────────────┘
```

### 2. 解锁页（每次启动）

```
┌─────────────────────────────────┐
│         🔐 Credential Vault     │
│                                 │
│   ┌─────────────────────────┐   │
│   │  输入主密码              │   │
│   └─────────────────────────┘   │
│                                 │
│        [ 解锁 ]                 │
│   错误提示：密码错误，请重试      │
└─────────────────────────────────┘
```

### 3. 凭据管理（Credentials）

左侧为插件目录树（与 `plugins/` 文件系统层次一致：类别 → 服务子插件），
点击类别节点显示该层级及其子层级的全部条目，点击服务节点按类型过滤；
右侧为凭据表格。**表头可拖拽调整列顺序**（顺序持久化到 localStorage）。

表格列：凭据（图标 + 名称 + 掩码敏感值副标题，如 `sk-123…cdef`）、类型、备注、
附加信息（余额等 account_info）、分组、状态、添加时间、最后验证、最后有效、操作。

```
┌──────────────────────────────────────────────────────────────────────┐
│  凭据管理  共 24 条            [ 批量验证 ] [ + 添加凭据 ]            │
│  筛选：[搜索] [ 全部分组 ▼ ] [ 全部状态 ▼ ] [ 重置 ]                 │
│ ┌──────────────┬───────────────────────────────────────────────────┐ │
│ │ 🗂️ 全部凭据   │ 凭据              备注      附加信息     状态     │ │
│ │ ├ 🤖 LLM     │ ├───────────────────────────────────────────────┤ │ │
│ │ │ ├ OpenAI   │ │ 🤖 OpenAI 生产账号      生产主账号  余额…    ✅ │ │
│ │ │ ├ DeepSeek │ │    sk-proj…3k9x                                │ │
│ │ │ └ …        │ │ 🤖 DeepSeek 主账号      —        余额 ¥97.8 ✅ │ │
│ │ └ 🔌 API 服务 │ │    sk-ddb1…f71e                               │ │
│ │   ├ Firecrawl│ │              [ 验证 ] [ 编辑 ] [ 🗑 ]           │ │
│ │   └ …        │ └───────────────────────────────────────────────┘ │ │
│ └──────────────┴───────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

**添加凭据**：表单的「服务」下拉支持**关键词搜索**，选项按插件类别分组
（LLM / API 服务）。保存前自动做**重复凭据检测**——凭据值与既有凭据任一字段
相同即弹出确认框，用户可选择继续添加或取消。

**添加/编辑凭据弹窗**（表单由插件 fields_schema 动态渲染）：

```
┌─────────────────────────────────────┐
│  添加凭据                      [×]  │
│                                     │
│  凭据名称 *                         │
│  ┌─────────────────────────────┐   │
│  │  OpenAI 生产账号             │   │
│  └─────────────────────────────┘   │
│                                     │
│  服务 *（可输入关键词搜索，按类别分组）  │
│  ┌─────────────────────────────┐   │
│  │  🤖 OpenAI              ▼   │   │
│  └─────────────────────────────┘   │
│                                     │
│  所属分组                           │
│  ┌─────────────────────────────┐   │
│  │  工作项目                ▼   │   │
│  └─────────────────────────────┘   │
│                                     │
│  ── 以下字段由插件定义 ──           │
│                                     │
│  Base URL                           │
│  ┌─────────────────────────────┐   │
│  │  https://api.openai.com/v1   │   │
│  └─────────────────────────────┘   │
│                                     │
│  API Key *                          │
│  ┌─────────────────────────────┐   │
│  │  ••••••••••••••••••••        │   │
│  └─────────────────────────────┘   │
│                                     │
│  备注                               │
│  ┌─────────────────────────────┐   │
│  │  生产环境主账号              │   │
│  └─────────────────────────────┘   │
│                                     │
│  ☑ 保存后自动验证                   │
│                                     │
│       [ 取消 ]   [ 保存 ]           │
└─────────────────────────────────────┘
```

### 4. 分组管理（Groups）

```
┌─────────────────────────────────────────────────────┐
│  分组管理                        [ + 新建分组 ]     │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │ 🔵 工作项目          5 条凭据               │   │
│  │    2026年Q4核心项目                    [ ⋮ ] │   │
│  ├─────────────────────────────────────────────┤   │
│  │ 🟢 个人              3 条凭据               │   │
│  │    个人测试用途                        [ ⋮ ] │   │
│  ├─────────────────────────────────────────────┤   │
│  │ ⚪ 未分组            2 条凭据               │   │
│  │                                        [ ⋮ ] │   │
│  └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### 5. MCP 设置（MCP）

```
┌─────────────────────────────────────────────────────────────┐
│  MCP 服务                                      🟢 运行中     │
│                                                             │
│  服务地址：http://localhost:8765/sse            [ 复制 ]     │
│  活跃 Token：1 个   传输协议：SSE   鉴权：Bearer Token      │
│                                                             │
│  访问 Token  cv_a3f2…  明文仅轮换时展示   [ 轮换 Token ]    │
│                                                             │
│  [ 停止服务 ]   [ 刷新 ]   [ 复制客户端配置 ]                │
│                                                             │
│  ── 客户端配置示例（点击复制）──                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ { "mcpServers": { "credential-vault": {             │   │
│  │     "url": "http://localhost:8765/sse",             │   │
│  │     "headers": { "Authorization": "Bearer cv_..." } │   │
│  │ } } }                                               │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘

── 工具权限（全局开关）──  关闭后所有 Token 均无法调用该工具

✅ vault_list_credentials    列出凭据
✅ vault_verify_credential   触发验证
✅ vault_list_groups         列出分组
✅ vault_list_plugins        列出插件
⬜ vault_get_credential      获取凭据详情（掩码）
⬜ vault_create_credential   创建凭据
⬜ vault_update_credential   修改凭据
⬜ vault_delete_credential   删除凭据
⬜ vault_batch_verify        批量验证
⬜ vault_create_group        创建分组
⬜ vault_get_secret          获取明文密钥（需二次确认）⚠️
```

> Token **默认即存在、且只需轮换**：不再有"生成 Token"入口，也不在 Token 上单独配置工具范围
> （统一由上面的「工具权限」全局开关控制）。

**轮换 Token**：点击「轮换 Token」→ 二次确认（"旧 Token 立即失效"）→ 展示一次性明文。

```
┌─────────────────────────────────────┐
│  ✅ Token 已轮换               [×]  │
│                                     │
│  ⚠️ 请立即复制，关闭后无法再次查看  │
│  ┌─────────────────────────────┐   │
│  │ cv_a3f2b1c9d8e7f6a5b4c3...  │ 📋 │
│  └─────────────────────────────┘   │
│                                     │
│            [ 我已保存，关闭 ]       │
└─────────────────────────────────────┘
```

### 6. 系统设置（Settings）

```
┌─────────────────────────────────────────────────────────────┐
│  系统设置                                                   │
│                                                             │
│  ── 安全 ──                                                 │
│  [ 修改主密码 ]                                             │
│  修改主密码将重新加密所有凭据，此过程不可中断。               │
│                                                             │
│  ── 验证行为 ──                                             │
│  ☑ 保存凭据后自动验证                                       │
│  验证超时：[ 15 ] 秒                                        │
│  批量验证并发数：[ 10 ]                                     │
│                                                             │
│  ── 定时验证 ──                          [ + 添加计划 ]     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 每日全量验证    每天 02:00    全部凭据    ✅ 启用     │   │
│  │ 上次运行：2026-10-03 02:00   下次：2026-10-04 02:00  │   │
│  │                                       [ 编辑 ] [ 删除 ] │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ── 数据 ──                                                 │
│  数据库位置：./data/credentials.db（项目目录内）             │
│  [ 导出备份（JSON） ]  [ 导入备份 ]                         │
│                                                             │
│  ── 关于 ──                                                 │
│  Credential Vault v0.1.0                                    │
│  Python 3.11 | pywebview 5.1 | Vue 3.4                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 系统托盘行为

应用支持最小化到系统托盘后台运行，MCP Server 和定时调度器持续工作。

### 窗口关闭行为

关闭窗口的行为由设置项 `close_action` 决定（"系统设置 → 窗口与关闭"可调整）：

| 取值 | 行为 |
|------|------|
| `ask`（默认） | 弹出确认框，让用户选择"直接退出"或"后台运行"，可勾选"记住我的选择" |
| `quit` | 直接退出程序（停止所有后台服务） |
| `background` | 隐藏窗口，最小化到托盘（MCP 与定时任务继续运行） |

| 操作 | 结果 |
|------|------|
| 托盘菜单"显示主窗口" | 恢复窗口显示 |
| 托盘菜单"锁定" | 清除内存密钥，下次显示窗口时需重新输入主密码 |
| 托盘菜单"退出" | 停止所有后台服务，真正退出进程 |

### 首次最小化提示

第一次点击 `×` 时弹出气泡通知：

```
Credential Vault
已最小化到托盘，MCP 服务持续运行。
右键托盘图标可退出。
```

### 托盘图标状态

| 图标 | 含义 |
|------|------|
| 🟢 | MCP 运行中，有活跃 Token 连接 |
| 🔵 | MCP 运行中，无活跃连接 |
| 🔴 | MCP 已停止或出错 |
| ⚪ | 应用未解锁（主密码未输入） |

### 前端与托盘的交互

前端通过 `window.pywebview.api` 控制托盘行为：

```javascript
// 最小化到托盘（前端调用，如用户点击"最小化"按钮）
await window.pywebview.api.minimize_to_tray()

// 锁定应用（清除内存密钥）
await window.pywebview.api.lock_vault()

// 查询托盘/MCP 状态
const status = await window.pywebview.api.get_tray_status()
// → { mcp_running: true, port: 8765, active_tokens: 1, is_locked: false }

// 关闭行为：读取 / 持久化设置（"系统设置 → 窗口与关闭"）
await window.pywebview.api.get_close_action()
await window.pywebview.api.set_close_action('background')

// 关闭确认弹窗中用户做出选择后回调（action: 'quit' | 'background'，remember=是否记住）
await window.pywebview.api.confirm_close_action('quit', true)
```

---

## Pinia Store 设计

### `useVaultStore`（核心状态）

```javascript
{
  // 状态
  isUnlocked: false,
  credentials: [],
  groups: [],
  plugins: [],
  filters: { group_id: null, credential_type: null, credential_types: null, status: null, search: '' },
  
  // 计算属性
  filteredCredentials: [],
  statusCounts: { total: 0, valid: 0, invalid: 0, unknown: 0 },
  
  // 操作
  unlock(password),
  setupMasterPassword(password),
  fetchCredentials(),
  createCredential(data),
  updateCredential(id, data),
  deleteCredential(id),
  verifyCredential(id),
  batchVerify(scope),
  fetchGroups(),
  createGroup(data),
  fetchPlugins(),           // 插件放入 plugins/ 目录即自动加载，无安装入口
  checkDuplicateCredential(type, data),  // 创建前重复检测
}
```

### `useMcpStore`（MCP 设置）

```javascript
{
  // 服务状态
  serverStatus: 'stopped',  // running | stopped | error
  port: 8765,

  // 全局工具权限
  permissions: {},           // { tool_name: bool }

  // 当前活跃 Token（全局唯一，默认即存在）
  token: null,               // { exists, id, token_prefix, created_at, last_used_at }

  // 操作
  fetchPermissions(),
  setPermission(toolName, enabled),
  fetchToken(),
  rotateToken(),             // 作废旧 Token 并签发新的，返回明文（仅一次）
  start() / stop(),
}
```

### `useTrayStore`（托盘状态）

```javascript
{
  mcpRunning: false,
  activeTokens: 0,
  isLocked: false,
  
  fetchStatus(),            // 调用 api.get_tray_status()
  minimizeToTray(),
  lockVault(),
}
```

---

## 前端事件监听

后端通过 `window.evaluate_js()` 推送以下事件：

| 事件名 | 触发时机 | detail |
|--------|----------|--------|
| `vault:credential_updated` | 凭据状态变化 | `{id, status, status_detail}` |
| `vault:credentials_changed` | 凭据增删 | `{}` |
| `vault:batch_verify_progress` | 批量验证进度 | `{done, total, current_id}` |
| `vault:batch_verify_done` | 批量验证完成 | `{results: [...]}` |
| `vault:mcp_status` | MCP 服务状态变化 | `{status, port}` |
| `vault:close_confirm` | 用户点击关闭且关闭行为为"询问" | `{}`（前端弹出退出/后台选择框） |
