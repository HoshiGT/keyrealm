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

**启动与单实例**（`backend/single_instance.py`）：`main()` 的**第一件事**就是取单实例锁
——Windows 用命名互斥体 `CreateMutexW`（内核在进程退出时自动释放，**不像 pid 文件那样会在
崩溃/被强杀后留下陈旧锁**），其它平台用 `data/.instance.lock` 上的 `fcntl.flock`。
取不到锁说明已有实例在跑：第二套进程**不创建界面**，而是 `FindWindowW("Credential Vault")`
+ `ShowWindow` + `SetForegroundWindow` 把已有窗口拉到前台，然后自己退出——用户感知上就是
「双击 exe 就出来了」。拿不到句柄/目录不可写等意外一律**放行**（宁可多开，也不能让应用起不来）。
互斥检查放在 `logger.setup_logging()` 之前，因为重复启动不该产生任何副作用；而
`--print-paths` 这个调试分支在 `main()` 之外，不受影响。

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
│   └── …                     # 共 19 个 LLM 子插件
├── api/                      # API 服务类别（firecrawl / tavily / metaso / coze，共 4 个）
├── dns/                      # 域名解析：dnspod / duckdns / godaddy / cloudflare
├── dev/                      # 开发者平台：github / huggingface / wandb
├── im/                       # 即时通讯：feishu / dingtalk / telegram / discord / qq_bot / wechat_ilink
├── cloud/                    # 云服务商：aliyun / tencentcloud
├── exchange/                 # 交易所：okx / binance
└── mail/                     # 邮箱：smtp（通用 SMTP，覆盖 Gmail/QQ/163/126）
```

每个类别目录结构一致：`category.json` + 可选 `<类别>_common.py` + 各服务子目录（`manifest.json` + `plugin.py`）。

- **一个服务 = 一个独立子插件**：各自持有 `manifest.json`（字段 schema、credential_type）与薄 `plugin.py`；删除任一子插件目录不影响其他子插件运行。根目录下直接含 `manifest.json` 的目录视为无类别的平铺插件。
- **类别共享模块**：`<类别>_common.py` 只放**底层通用工具 + 子插件原型接口**（`VerifyResult` / `make_client` / `safe_json` / `guarded` 等）；**协议级工具**（如 `plugins/llm/llm_protocols.py` 的探针与 `chat_completion`）另建独立工具模块供子插件按需引用。加载子插件时临时将其目录与类别目录注入 `sys.path`（见 plugin_base.load_plugin_module）。**服务专属逻辑在各子插件内自带，新增插件无需改动共享模块。**
- **同步流程**：启动时 `sync_plugins()` 扫描两层结构 → upsert plugins 表（含 category / category_name / batch_verify）→ 清理已消失插件的记录与模块缓存。
- **批量验证开关**：manifest 的 `batch_verify`（默认 true）落库到 plugins.batch_verify；调度器与前端批量时跳过为 false 的类型（无法自动化或有写副作用，如 duckdns / wechat_ilink）——前端在批量勾选弹窗中禁用其勾选。

**插件模块约定**：必须暴露 `verify(data)`；可选 `validate(data)`、`get_display_summary(data)`。
`verify` 返回值鸭子类型兼容 VerifyResult / dataclass / dict。

**「操作」框架（`actions` / `run_action` / `capabilities` / `stream_action`）**：插件可选声明一组操作，
前端凭据行的「操作」按钮据此渲染。数据流：

```
前端 openActions(row)
  ├─ list_credential_actions(id)   → plugin_manager.plugin_actions（纯本地，渲染菜单）
  └─ credential_capabilities(id)   → plugin_manager.plugin_capabilities → 插件的 capabilities()
                                      （会联网探权限，包在代理作用域里；失败一律 fail-open）
        │
        ├─ 1 个操作 → 直接进；≥2 个 → 弹选择菜单，探测结果回来再收敛
        ▼
  run_credential_action(id, action_id, params)
        → plugin_manager.run_plugin_action → 插件的 run_action(action_id, params, data)
        → 结果经 _redact_deep 递归净化后才回前端
```

三个设计要点：

- **`actions()` 必须便宜且无副作用**，`capabilities()` 才是联网探测的那个 —— 分开才能把
  「没有操作 / 有操作但没权限 / 探测失败」三种状态说清楚（混在一起时，探测失败会伪装成「没有操作」，用户
  就分不清该去修密钥还是修网络）。
- **`kind: "list"`** 让插件用 `columns` + `rows` + `row_actions` 描述一个表格，行内按钮的 `params`
  支持 `$row.<列key>` 模板，目标 action 是 `form` 就预填、是 `list` 就下钻（前端用栈保存上一帧，返回零请求）。
- **净化边界在 `backend/api.py`**：`_redact_deep` 递归遍历**整个结果**（`output`/`error`/`rows`/任意嵌套）
  把该凭据 `sensitive` 字段的明文替换掉。早先只扫顶层两个字段，列表类操作一上来就会把密钥从 `rows` 里漏出去。
- **下载类操作**（`save_to`）：前端先弹「另存为」拿路径，路径经 `params` 交给插件，**插件自己下载**
  —— 不让插件返回 `{url, headers}` 由后端代下，是因为那会把鉴权头/预签名 URL 送进结果 dict 并穿到 JS 桥。
  对话框只在 `backend/api.py` 里弹：流式路径的插件跑在新建线程上，跨线程弹窗会卡界面。

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
    ▼ 并发调用各插件的 verify()（每条凭据一个任务，并发数可配，见下）
    │
    ▼ 更新 credentials.status / last_verified_at
    │   （status == valid 时同时更新 last_valid_at）
    │
    ▼ 通过 pywebview 事件通知前端刷新
```

**验证代理（多代理 + 按调用生效）**：代理存在 `proxies` 表里、各自取名，**其它地方一律按 id 引用**
（`proxy_store`）。id 是 **UUID 而非自增整数**——自增只在单库唯一，备份导入（代理按原样沿用 id 重建）
后一旦对不上，凭据/预设里的引用会静默悬空 → 回落直连，界面上却看不出异常。解析时**按优先级逐层找**，
第一层给出可用的指定就停：

| 优先级 | 来源 | 怎么设 |
|---|---|---|
| 1 | **本条凭据** | 凭据编辑表单里的「使用代理」（`data._proxy_mode` / `_proxy_id`） |
| 2 | **子模块预设** | 该插件的默认设置（`preset_defaults` scope=`plugin`） |
| 3 | **上层模块预设** | 所属类别的默认设置（scope=`category`） |
| 4 | **全局** | 「系统设置 → 网络与代理」的默认代理（写进 `settings.global_proxy_id` + 环境变量 `VAULT_PROXY`） |

第 1 层里「跟随上层设置」（`_proxy_mode = global` 或缺省）**不表示「用全局」**，而是「本层不指定，
继续往上找」——所以改了插件/类别的预设，已有凭据会跟着变。**引用的代理被删了也一样往上找**
（不直接掉到全局，更不报错）。第 2、3 层的合并顺序由 `preset_store.defaults_for()` 保证（插件覆盖类别）。

难点在于**批量验证是并发的**：不同凭据可能要走不同出口，而插件只认进程级环境变量
`VAULT_PROXY`，直接改环境变量会互相踩。所以引入了一条**按调用生效**的通道：

- `backend/vault_proxy_ctx.py`（框架持有）用 `threading.local()` 存「本次调用用哪个代理」，
  两个哨兵 `UNSET`（未指定 → 回退环境变量）与 `DIRECT`（强制直连、压过所有代理变量）
  必须分开——用同一个 `None` 表达会让「跟随全局」在外部设了 `ALL_PROXY` 时无法正确回退。
- `main.py` 启动时把它以**固定别名**注册进 `sys.modules`：`sys.modules["vault_proxy_ctx"]`。
  各 `<类别>_common.py` 的 `proxy()` **懒查**这个名字（插件不允许 import backend，
  且脱离主程序单独跑时自然取不到 → 退回环境变量），因此**插件代码一行都不用改**。
- 收口在 `plugin_manager` 的四个入口（`verify_credential` / `run_plugin_action` /
  `plugin_models` / `stream_plugin_action`）用 `credential_proxy_scope(data)` 包住，
  于是单条 / 批量 / MCP / 流式四条路径全自动覆盖。**必须 `begin/end(token)` 成对还原**：
  `ThreadPoolExecutor` 会复用线程，不还原就会把上一条凭据的代理带给下一条。
  `stream_credential_action` 另开线程，作用域因此只能建在它调的 plugin_manager 里，
  建在调用线程里会静默失效。

进程环境变量那条通道仍然保留（`backend/proxy.py` 的 `apply_proxy()`），给没走 ctx 的场景兜底。

**类别 / 插件默认设置（`backend/preset_store.py`）**：新建凭据时的预填值，存
`preset_defaults` 表，按「插件级 > 类别级 > manifest 的 `field.default`」继承。框架只做
**存 + 继承 + 注入**三件事，「哪些字段可以预设」由插件自己在 manifest 里声明——默认全部
**非敏感**字段都可预设，加 `"preset": false` 可排除；`api_key` 之类的敏感字段**永远不进默认值**
（`values` 是明文列，写入 / 读取 / 注入三处各剔一次）。注入点有三处且**缺一不可**：
前端新增表单（`CredentialForm.loadDefaults`）、`api.create_credential`（**在校验之前**，
默认值可能正好满足必填项）、以及 MCP 的 `vault_create_credential`——后者原本是复制出来的
第二份创建逻辑，现已改为直接调用 `api.create_credential`，避免「表单继承、MCP 不继承」这类
不一致再次出现。**更新（update）一律不套默认**，否则用户显式清空的字段会被复活。

**运行参数（`backend/verify_options.py`）**：批量验证的**并发线程数**（默认 8，1–32）与**请求超时**（默认 20 秒，5–300 秒）在「批量验证」对话框里就地调整，是这两项的**单一事实来源**——持久化到 `settings` 表的 `batch_concurrency` / `verify_timeout`，前端取值范围也由它下发（`limits()`），避免前后端各写一份常量。两条消费路径：

- **并发数**：`scheduler.batch_verify()` 内层 `ThreadPoolExecutor(max_workers=verify_options.concurrency(...))`，取 `min(并发设置, 待验证条数)`；手动批量传本次选择，定时任务不传 → 读已保存的设置。
- **登记成后台任务**：批量验证会建一条 `kind=batch_verify` 的 `tasks` 任务，把 done/total 喂给任务表
  （「任务」页因此能看到进度并**随时取消**）。取消是协作式的：消费循环每收一个结果前查一次
  `tasks.should_cancel()`，命中就 `pool.shutdown(wait=False, cancel_futures=True)` 收尾
  （正在跑的那几个让它自己跑完，不强杀线程），任务标成「已取消」并如实写「已处理 n/m」。
- **三个事件**（前端据此做横幅与行内 loading，不用模态弹窗把人按在前台）：
  `vault:batch_verify_started`（带本次要验证的凭据 id 列表**与触发方 `trigger`**——`manual` 才让
  前端整页互斥，`mcp` / `schedule` 是用户没看着时跑起来的，只提示不锁页）、`..._progress`、`..._done`
  （取消时额外带 `canceled` 与 `done`）。
- **超时**：同样借环境变量下发（插件不允许 `import backend`）——`apply_timeout()` 把值写入 `VAULT_TIMEOUT`，插件侧 `timeout()` 每次发请求时读取，改完立即生效。外部显式设置的同名变量优先。机制与 `VAULT_PROXY` 同构，见 `docs/PLUGIN_GUIDE.md` 第 2.3 节。

**单条验证的并发保护**：`verify_single_credential()` 外层套了一个**同 id in-flight 集合**
（`_inflight` + 锁），同一凭据已在验证中时直接返回「该凭据正在验证中，请稍候」而不重复发请求。
批量验证走的也是这个函数，所以「手动点验证」与「定时/批量」撞车时同样只会跑一次。
前端另有一层：验证状态记的是 **id 列表**而非单个 id，逐行独立 loading。

**欠费探测后置检查（`plugin_manager._apply_quota_probe`）**：一次凭据验证的主流程是
`api/scheduler → plugin_manager.verify_credential() → call_verify(module, data)`；返回前多一道**可选**检查——
仅当结果 `valid`、且 `account_info` 里既无 `balance` 也无 `usage_rate` 时，才调用插件的
`quota_probe(data)`（未接入该函数的插件直接跳过）。这是「Key 有效」与「账户有钱」之间的补丁：
`/models` 探针对欠费账户同样返回 200，只有真实发一次 `max_tokens=1` 的对话才能分辨。开关与模型是
**凭据自身**的字段（manifest 的 `probe_chat` / `probe_model`，随 `data` 一起进插件），逐条凭据独立、
默认关闭——**不经过全局设置，也不需要环境变量**（数据本来就在插件手里）。后置检查里的任何异常都被
吞掉并只写日志——**它绝不允许影响主验证结果**。详见 `docs/PLUGIN_GUIDE.md` 第 2.4 节。

**代理连通性诊断**（`backend/proxy_test.py`）：代理字符串本身看不出好坏，故做一次**对照实验**——同一批探测分别在「直连」与「走代理」下各跑一遍，再比对：① 国内站点（baidu.com）可达性 ② 国外站点（gstatic.com/generate_204）可达性 ③ 出口 IP 与归属地。综合给出 `ok / warn / fail` 结论（如「出口 IP 已切换」「经代理国内外站点均不可达」「出口 IP 与直连相同：代理可能未生效」）。

两个刻意的设计：`trust_env=False` 屏蔽系统 `HTTP(S)_PROXY`/`ALL_PROXY`，否则「直连」对照组会被环境代理污染；出口 IP **只认无国内 CDN 的 HTTPS 见证源**（ipify / icanhazip / ifconfig.me / ipinfo.io）——最初首选 ip-api.com，实测它在国内有 CDN 且被规则型代理客户端判为直连，导致「挂代理」读到本机真实 IP、结论反向，故 ip-api 仅用于查归属地（IP 写在路径里，与路由无关）。六项探测并发执行，单项失败不影响其它项。

**缺 SOCKS 依赖的处理**：httpx 走 socks 需要独立的 `socksio` 包，而它是在 httpx 内部**函数体内** `import` 的——打包时构建机没装，PyInstaller 既不报错也不打包，直到运行时用 socks5 代理才抛一句英文 `ImportError`。这条链路做成三层兜底：

| 层 | 位置 | 行为 |
|----|------|------|
| 构建期 | `scripts/build.py` / `CredentialVault.spec` | 构建前按 `requirements.txt`（**含 extras 展开**）核对当前环境，缺包就打印清单并**中止**（`--allow-missing-deps` 可放行），提示 `pip install -r requirements.txt`；spec 里显式 `collect_all("socksio")`，直接用 `pyinstaller CredentialVault.spec` 时也会警告 |
| 诊断 | `backend/proxy_test.py` | `proxy_config.needs_socks()` + `socks_available()` 提前判定，socks 代理 + 缺依赖时**不发请求直接短路**，给出 `fail` 结论与中文处理办法（伪造一份代理侧探测结构，界面两列卡片照常渲染） |
| 插件 | `backend/plugin_base.py` | `_friendly_error()` 把异常文本里的 socksio 报错换成中文处理办法；原始异常仍完整写入日志，不缺排查线索 |

判定与文案集中在 `backend/proxy.py`：`needs_socks()` / `socks_available()` / `SOCKS_HINT`（单一事实来源，三层共用）。

**备份 / 恢复**：`backend/backup.py` 负责构建/导入备份文档（JSON）。加密备份的敏感字段直接沿用库中密文，只能用同一主密码导入；明文备份可跨主密码导入（以当前主密钥重新加密写库）。导入按「类型 + 敏感值」去重，并跳过插件未安装的条目；文件读写通过 pywebview 的原生文件对话框（`api._file_dialog`，优先用 `webview.FileDialog` 枚举——旧的模块级 `SAVE_DIALOG`/`OPEN_DIALOG` 常量会打印弃用警告）。

导出有两个**互不重叠**的维度：`scope` 决定是否附带**分组与偏好设置**（`all` 带、`credentials` 不带），`credential_ids` 决定**导出哪些凭据**（`None` = 全部，给定列表 = 只导这些；不影响 scope 语义）。因此「全部 + 勾选若干」与「仅凭据 + 勾选若干」的差别仍然只是分组/偏好设置那部分，文档里写入 `credential_ids` 便于回溯。前端「备份」页把两者做成两组控件（范围单选 + 凭据勾选列表）。

---

### 2.5 MCP Server（mcp_server.py）

**职责**：为 AI Agent 提供凭据管理接口。

**传输**：SSE（Server-Sent Events），默认监听 `127.0.0.1:8765`。不随应用自动启动，由用户在「MCP 设置」页手动开启；开启后窗口最小化到托盘仍持续运行。

**监听地址可配**：`settings` 的 `mcp_host` / `mcp_port`（默认 `127.0.0.1` / `8765`）。
`start_server(host, port)` 不再把 `127.0.0.1` 写死，也**去掉了原来的「端口被占就 +1/+2」降级**
——端口是用户显式配的，悄悄换一个会让人对着「配了 9000 却在 9001」的界面发懵；现在占用就直接
报错。保存设置时若服务在运行，会先停、等端口真正释放、再按新地址起；**若新端口起不来，会把
原地址的服务起回去**并返回错误，不把用户留在「改完啥都没了」的状态。host 只接受合法 IP 或
`localhost`（防止误把凭据服务挂到公网）。

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

**托盘线程**：`pystray` 在独立守护线程中运行，通过线程安全队列与主线程通信。

---

### 2.8 日志模块（logger.py）

**职责**：统一日志配置，写入 `<app_dir>/data/logs/credential_vault.log`，并可按用户选择调整级别。

**实现**：`logging` + `RotatingFileHandler`（单文件约 1 MB × 5 份备份），多行堆栈按时间戳归组。

**日志级别**：`DEBUG / INFO / WARNING / ERROR / CRITICAL`（默认 `INFO`），运行时可调整并持久化到
`settings` 表的 `log_level` 键，下次启动自动恢复。`httpx` / `httpcore` 被压制到 `WARNING`，
避免第三方库噪声。

**检索**：`read_logs(limit, level, search)` 支持最低级别过滤 + 关键词搜索。搜索按「整条记录」
匹配（含 traceback 续行），大小写不敏感，多个空白分隔的关键词需**全部命中**（AND）。

**采集范围**：插件不再用 `print(..., file=sys.stderr)` 输出——打包为 windowed 模式时
`sys.stdout`/`sys.stderr` 为 `None`，打印会静默丢失。改为各 `<类别>_common.py` 内的
`_logger = logging.getLogger("plugin.<类别>")`，异常经 `backend/plugin_base.py` 的
`call_*` 辅助函数统一 `_log.exception` 落盘（原先这些异常被静默吞掉，是插件报错不进日志的根因）。
调度器 `scheduler.verify_single_credential` 逐条记录验证结果并在批量结束时汇总一条统计。

**界面**：日志是独立页面（`frontend/src/views/Logs.vue`，侧栏「日志」），不再挂在「系统设置」下。
页面提供级别筛选、关键词搜索、显示条数、清空与刷新。

---

### 2.9 后台任务（tasks.py）

**职责**：让「点一下就开跑」的长操作（OSS 下载 / 前缀打包等）**看得见、能终止、关掉弹窗不丢结果**。

**为什么单独一个模块**：这类操作跑在后台线程里，弹窗一关就没人接收进度事件了。任务注册表把
状态从「某个弹窗的事件流」提升为「全局可查询的状态」。

- **纯状态模块**：只做任务的增删改查 + 取消标志，不认 webview、不认数据库。推送由 `api.py`
  注入（`tasks.set_notifier(push_event)`）——既避免循环依赖，也让本模块能脱离应用单独测试。
- **不持久化**：任务是运行期状态，进程重启即失效（重启后也没有线程在跑了）。已结束的只保留
  最近 `_MAX_FINISHED = 50` 条，免得长时间运行越攒越多。
- **状态**：`running` / `done` / `failed` / `canceled`，外加 `cancel_requested`（已点取消、
  正在等插件停下）。

**取消是协作式的**：`cancel()` 只置一个 `threading.Event`，真正停下靠**插件**在下载循环里查
`should_cancel()`（框架把它按关键字传给 `stream_action`，与 `on_progress` 同一套「签名里有才传」的
机制）。所以做不到立刻杀线程，但会在下一个检查点（每个 chunk / 每个对象）停下并清理 `.part`。
「取消还是失败」由**框架**判断（它持有标志），不靠插件猜错误文案——刚好在取消前跑完的仍算成功。

**事件与界面**：任何变更推一次 `vault:tasks`（detail 是变化的那条快照）。前端「任务」页据此
就地更新，侧栏「任务」项带进行中数量角标。**带 `progress: true` 的操作自动成为任务**，所以
弹窗里多了一个「转到后台」——关掉弹窗任务照跑。

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
