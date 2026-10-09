# Credential Vault — 凭据管理与验证平台

一个本地优先的桌面应用，用于集中管理、加密存储和主动验证各类 API 凭据。

## 特性

- **AES-256-GCM 加密存储**：主密码派生密钥，敏感字段全程加密
- **插件化架构**：一个服务一个插件，目录放入 `plugins/` 即自动加载，删除即卸载，无需手动管理
- **插件管理**：`plugins/` 是插件的**唯一事实来源**（放入即自动加载、删除目录即卸载，凭据保留）——一页列出全部插件，每行**左侧常驻「已启用」/「已禁用」状态标签**，对任一插件**禁用 / 启用**（状态存 `plugins/.state.json`，禁用**不改动磁盘目录**，只让该插件「隐形」：其功能选项在界面自动置灰、批量验证自动跳过，名下凭据的状态显示为**「插件禁用」**（派生状态，重新启用即自动回弹，不写进库）、也不许再给它**添加凭据**，而编辑/删除照常可用）；按**类别折叠**（搜索时自动展开命中类别）让长列表不再难扫；支持按名称 / 标识 / 描述**实时搜索**（空格分隔多词需全部命中）；凭据目录里右键节点即可**禁用 / 启用插件**或配置该类别/插件的默认设置
- **MCP 服务**：内置 MCP Server，**监听地址可配**（IP + 端口，默认 `127.0.0.1:8765`；端口被占直接报错、不悄悄换端口），AI Agent 可直接管理与验证凭据，支持按工具粒度控制权限；**备份导出/导入也可由 MCP 调用**（`vault_export_backup` / `vault_import_backup`，默认关闭）——没有文件对话框，改为「文档直接返回」或「显式给路径」两种方式
- **多模式验证**：保存时自动验证 / 手动验证 / 定时批量验证；定时计划的**验证范围**可选**全部凭据 / 指定分组 / 指定类型 / 指定凭据**（后者与「备份 → 仅选中的凭据」**复用同一套凭据目录树勾选**，类别 → 服务 → 凭据三层、可搜索、全选只勾当前可见项）；批量验证可**手动勾选**（支持一键全选）并**并发**执行，**并发线程数与请求超时在批量验证对话框里就地调整**（并发 1–32、超时 5–300 秒），保存后对定时批量验证同样生效；有写副作用或无法自动化的类型可声明 `batch_verify: false`，仅允许手动单个验证；**被禁用的插件同样自动跳过定时与批量验证**（其凭据状态显示为「插件禁用」）；对**拿不到余额**的凭据（厂商无余额接口，或中转站令牌为「不限额度」），可**在该凭据上单独开启**「欠费探测」——额外发一次最小对话请求，把「Key 有效」升级为「实测可用 / 实测欠费」，默认关闭
- **可读的附加信息**：凭据列表「附加信息」列以「标签 值」展示——LLM 显示余额（plan 类显示使用率），云厂商显示账号信息，无效字段自动隐藏；金额先过**合理性闸**，中转站「不限额」哨兵值（`1e8`）不会渲染成 `99999996.47` 这种假余额
- **多代理 + 按凭据选出口**：可维护**多个代理**（各自取名，其它地方一律按 id 引用），设一个全局默认；**每条凭据**可三选一——跟随上层设置 / 指定某个代理 / 直连；解析优先级是 **本条凭据 > 该插件预设 > 所属类别预设 > 全局默认**。批量验证并发时各凭据互不串（框架按调用注入、线程隔离，插件无需改动）。「网络与代理」里可对任一代理一键**对照测试**：同一批探测分别走直连与代理，比对出口 IP 归属与国内外站点可达性，直接给出「代理是否真的生效」的结论（测试状态在 store 里，**切页不会丢结果**）。socks5 需 `socksio`（`requirements.txt` 已声明 `httpx[socks]`）：环境缺它时不再甩英文异常，而是直接给中文处理办法。**邮箱（SMTP/IMAP）同样支持代理**——它走的是裸 TCP，吃不了 HTTP 代理，改用 SOCKS / HTTP CONNECT **隧道**穿出去（依赖 `PySocks`）
- **类别 / 插件默认设置**：可给一个**类别**（如 LLM）或某个**插件**预设新建凭据的默认值（Base URL、备注、走哪个代理…），**插件级覆盖类别级**；新建凭据时自动带出。框架只提供存与继承，可预设哪些字段由插件自行声明，**敏感字段永不参与**
- **只允许一套在跑**：重复双击 exe 不会开出第二个实例，而是把已运行的那个窗口拉到前台
- **独立日志模块**：插件与后端的 INFO/告警/异常统一落盘（`data/logs/credential_vault.log`，1MB×5 轮转），独立页面可调级别、按关键词**搜索**（支持 AND 多词、含 traceback 的整条记录匹配）、清空与刷新
- **凭据简易操作**：插件可声明可选操作，凭据行出现「操作」按钮，按 `kind` 分三种形态——**对话**（流式返回，报错以红色气泡留在对话流里）、**表单**（写操作提交前二次确认）、**列表**（表格 + 行内按钮 + 下钻，例如「域名列表 → 该域名的解析记录」）。一个凭据有多个操作时先弹选择菜单，**只有一个就直接进**。下载类操作（如 GitHub 仓库打包）会弹系统「另存为」并原子落盘
- **按权限显隐操作**：插件可实现 `capabilities()` 探测该凭据真正能用哪些操作——**没权限的直接隐藏**，一个都没有时给出说明；探测失败一律 fail-open（宁可全显示，也不因一次网络抖动把能用的功能藏掉）
- **后台任务**：下载 / 打包这类长操作会登记成**后台任务**——弹窗里能看实时进度（`12.3 MB / 45.0 MB`、`3/12 对象名`），**点「转到后台」关掉弹窗照样跑**，侧栏「任务」页随时看进度、**随时取消**（协作式：在下一个检查点停下并清理未完成的临时文件），也可以一键清空已完成的任务记录
- **模型列表常驻**：LLM 的「简单对话」里模型列表按钮一直在、随时可刷新；拉到后只在用户没选过时自动取第一个，**已有选择绝不覆写**
- **备份 / 恢复**：导出为单个 JSON 文件（可选加密——默认保持敏感字段密文、需同一主密码导入；或明文导出、可跨主密码导入），**可选「全部凭据（含分组与偏好设置）」或「仅选中的凭据」**（后者用**凭据目录树勾选**：类别 → 服务 → 凭据三层，勾类别即全选其下，支持搜索过滤与已选计数；与定时计划的「指定凭据」共用同一个组件），导入自动识别二者并按敏感值去重
- **凭据列表排序与分页**：工具栏上「排序依据」（添加时间 / 名称 / 状态 / 最后验证 / 最后有效 / 分组）与「升序 / 降序」两个下拉分开选（记在本地界面偏好里）；列表**默认每页 10 条**（预设 5 / 10 / 20 / 50，也可**直接手输自定义条数**），**翻页控件在筛选框里整体靠右** —— 最右是「← / →」翻页按钮夹着当前页数，其左是「每页 xx 条」下拉，每页条数同样记在本地界面偏好里；逐条验证带并发保护，重复点击同一凭据不会重复发请求
- **分组管理**：平面分组，凭据按项目或用途归类
- **可自定义侧边栏**：菜单项直接拖拽即可调整顺序（拖动时显示插入线），顺序存在本地界面偏好里；折叠／恢复默认顺序按钮在**品牌下方的横排工具条**上、只显示图标、悬浮出提示（右侧留了给后续功能的占位），「恢复默认顺序」带二次确认。新增页面自动追加到末尾，不会因顺序记录而丢菜单
- **6 套界面主题**：3 套浅色（极光 / 素笺 / 薄荷）+ 3 套深色（午夜 / 黑曜 / 星云），在**「系统设置 → 外观」**里点一下即时换肤；选择与侧边栏顺序一样存在本地界面偏好里，不进数据库
- **跨平台**：Windows 优先，兼容 macOS / Linux

## 技术栈

| 层次 | 技术 |
|------|------|
| GUI 框架 | pywebview（系统 WebView） |
| 前端 | Vue 3 + Element Plus + Vite |
| 后端 | Python 3.11+ |
| 数据库 | SQLite（AES-256-GCM 字段级加密） |
| 加密 | cryptography（PBKDF2 + AES-GCM） |
| MCP | FastMCP（SSE 传输） |
| 定时任务 | APScheduler |
| HTTP 客户端 | httpx |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 构建前端
cd frontend && npm install && npm run build && cd ..

# 启动应用
python main.py
```

## 打包为可执行文件

打包为单文件可执行程序（Windows 为 `CredentialVault.exe`），输出到 `release/`。
**PyInstaller 不能跨平台编译，要得到 Windows exe 必须在 Windows 上执行构建。**

```bash
# 1) 安装打包工具（在应用所用 Python 环境中）
pip install pyinstaller

# 2) 构建前端 + 打包（一条命令）
python scripts/build.py --frontend

# 常用选项
#   --frontend            先执行 npm run build 再打包
#   --console             保留控制台窗口（调试用）
#   --smoke               构建后运行 --print-paths 校验冻结路径（隐含 --console）
#   --allow-missing-deps  依赖缺失时仍继续构建（默认中止；同时透传给 spec）
```

> **务必用「装好依赖的那个解释器」跑打包**——构建脚本会用它来调 PyInstaller 与做依赖检查，
> 跑错解释器会出现「刚 pip install 过却仍报缺包」。Windows 上尤其容易遇到：PowerShell 里激活
> `.venv` 后，`python` 走 venv，而 **`py` / `py3.14` 这类启动器不用 venv**（固定选系统 Python）。
> 拿不准时看构建输出里的 `解释器     : …` 那一行，或直接点名
> `.venv\Scripts\python.exe scripts/build.py --frontend`。

> **exe 图标**：由 `assets/icon.ico` 提供（`scripts/make_icon.py` 生成，已入库）。构建日志会打印
> `[spec] 应用图标：…`；若打印的是警告，则图标缺失，exe 会回退 PyInstaller 默认图标。
> 构建已关闭 UPX（UPX 会破坏 exe 内嵌图标资源）。若替换 exe 后资源管理器仍显示旧图标，
> 属 Windows 图标缓存，可执行 `ie4uinit.exe -show`，或把 exe 复制到新目录后再查看。
> 分不清是「图标没进 exe」还是「图标缓存」时，先跑 `python scripts/check_exe_icon.py`
> ——它解析 exe 的 PE 资源目录（RT_GROUP_ICON / RT_ICON），逐帧比对 `assets/icon.ico`，
> 直接给出结论和对应修法。**不能用「在 exe 里搜图标字节」来代替**：spec 把整个 `assets/`
> 当数据文件打进了包，所以 ico 的字节本来就存在于 exe 中，按字节搜必然命中，会得出错误结论。
>
> `icon.ico` 为**混合格式**：16–128 存 BMP(DIB)、256 存 PNG。Windows 仅对 256×256 保证支持
> PNG 压缩条目，小尺寸用 PNG 时任务栏/资源管理器可能解码失败而回退默认图标——故
> `make_icon.py` 手工组装并自检条目格式，**勿**改回 `Image.save(format="ICO")` 默认行为（它把所有帧都写成 PNG）。
>
> **运行时窗口/任务栏图标**：`assets/` 随包进 `resource_dir()/assets/`，`main.py` 启动时显式
> `webview.start(icon=<...>/assets/icon.ico)`。不再依赖 pywebview 从 exe 提取（提取到的多为
> 16px 小图，高 DPI 任务栏下模糊）。托盘图标是**状态色徽标**（绿/蓝/红/灰），非应用 logo。

构建完成后 `release/` 结构：

```
release/
├── CredentialVault.exe        # 单文件可执行程序（后端代码 + frontend/dist 已内置）
├── credential-vault-src.zip   # 源码归档（由 scripts/pack_source.py 生成，见下）
├── plugins/                   # 插件【唯一事实来源】（不进 exe，可直接编辑，重启生效）；
│                              #   .state.json 记录启用/禁用状态（不存在 = 全部启用）
│   ├── llm/                   # LLM 类别目录（category.json；llm_common.py 为类别特有辅助）
│   │   ├── openai/            # 每个服务一个独立子插件（manifest.json + plugin.py）
│   │   ├── deepseek/
│   │   └── …                  # 共 18 个 LLM 子插件
│   ├── api/                   # API 服务类别（category.json）
│   │   ├── firecrawl/
│   │   ├── tavily/
│   │   ├── metaso/
│   │   ├── coze/
│   │   ├── dify/
│   │   ├── fofa/
│   │   ├── google_api_credentials/
│   │   └── gateway/
│   └── dns/ dev/ im/ cloud/ exchange/ mail/   # 域名解析 / 开发者平台 / 即时通讯 / 云服务商 / 交易所 / 邮箱
└── data/                      # 首次运行时自动创建（数据库、主密码校验、日志；升级时保留）
    ├── credentials.db         # 加密凭据库（-wal / -shm 运行时生成）
    ├── logs/                  # 轮转日志（1MB × 5）
    └── webview/               # pywebview/WebView2 用户数据目录：界面偏好（列宽、树展开、可见列）
```

> 约定：插件只以 `plugins/` 为准。一个服务就是一个独立子插件，放入 `plugins/` 目录
> 即自动加载，删除对应子插件目录即卸载，互不影响，无需手动管理。**临时停用**插件无需删目录——
> 在「插件管理」里禁用它即可（状态记在 `plugins/.state.json`，可随时再启用）。

打包边界：

| 内容 | 是否进 exe | 说明 |
|------|-----------|------|
| 后端代码、`frontend/dist` | ✅ 内置 | 只读资源，运行时从临时解压目录读取 |
| `plugins/` | ❌ 外部 | 插件唯一事实来源，放 exe 同级；启动时扫描自动加载，删除子插件目录即卸载 |
| `data/` | ❌ 外部 | 运行时可写，放 exe 同级，保证数据持久化 |

`release/` 是自包含、可便携分发的目录，且**不被 git 追踪**（`CredentialVault.spec` 与
`scripts/*.py` 作为构建配置保留在版本库中）。

> **升级时 `data/` 原样保留**：`scripts/build.py` 只替换 exe、`plugins/`（且不覆盖 `plugins/.state.json`，
> 你的启用/禁用状态不会被重置），
> 在 `data/` 下仅清理旧版遗留的 `data/installed_plugins/`（zip 安装时代的运行期缓存）。
> 其余内容一律不碰——`data/webview/` 是界面偏好、`data/logs/` 是日志，升级前后都不用手工维护。

> **插件类型改名时（1.0 前不自动兼容）**：若某插件的 `credential_type` 变过，老库里该类型的
> 凭据会**找不到插件**（列表里显示不出来、也不能验证）。例如「字节豆包」已泛化为「火山方舟」，
> `llm_doubao` → `llm_ark`。这类改名用一次性脚本迁移：
> `python scripts/migrate_doubao_to_ark.py`（**默认只演练、不改任何东西**；加 `--apply` 才写库，
> 写前自动用 SQLite backup API 落一份一致性快照）。跑之前请**完全退出应用**——脚本会先抢一次
> 单实例锁来确认（抢不到就拒绝执行）。

> **版本规则（1.0.0 起）**：末位 +1 = 修 bug / 优化；中位 +1 = 新增特性 / 功能；
> 首位 +1 = 较大型的框架变动。**版本号只在明确要求时才提升**——其余提交照常，只写 commit、
> 不动版本号；**每次提升都必须同步更新 [`CHANGELOG.md`](CHANGELOG.md)**（独立更新日志）。
> **数据库结构有变动时，必须在 `scripts/` 附一份迁移脚本**（`migrate_*.py`）并同步提升
> `SCHEMA_VERSION` —— 应用内**不再新增**自动迁移（`database._migrate()` 里那几段只服务
> 1.0 之前的旧库，已冻结）。
> 版本号与「数据库结构版本」当前都是 **1.0.0**，界面上以 `1.0.0 (<commit>)` 展示 ——
> 括号里是**构建时**的 commit 短 hash，由 `scripts/build.py` 打包前注入（源码运行则现查 git）。

> **构建前的建表自检**：`scripts/build.py` 会在打包前跑一次 `scripts/check_schema.py` —— 在临时库上
> **真跑一遍建表与各表 CRUD**。建表 SQL 只有真连上 SQLite 才会被解析，而单元测试通常把 `database`
> 模块整个打桩，所以「SQL 写错」会一路测试全绿、打包后运行才崩（例如把 SQLite 保留字
> `values` 当成列名 → 建表 `syntax error`）。改了表结构后也可以单独跑 `python scripts/check_schema.py`。
>
> **构建前的依赖检查**：`scripts/build.py` 一上来就按 `requirements.txt`（含 extras 展开）核对
> 当前 Python 环境，缺包就打印清单并**中止**，提示 `pip install -r requirements.txt`
> （确实要忽略可加 `--allow-missing-deps`）。之所以要查 extras：httpx 是在**函数体内**
> `import socksio` 的，构建机没装时 PyInstaller 既不报错也不打包，问题直到 exe 里用 socks5
> 代理才暴露（`the 'socksio' package is not installed`）。
> **`CredentialVault.spec` 自己还会再核对一遍**那几个「静态分析看不到」的依赖（socksio /
> PySocks / telethon / python-socks），**缺一个就中止构建**，避免产出一个悄悄缺功能的包
> （直接用 `pyinstaller CredentialVault.spec` 打包同样会停）。逃生口是环境变量
> `CV_ALLOW_MISSING_DEPS=1`，`scripts/build.py --allow-missing-deps` 会自动设上，两边口径一致。
>
> **插件用到的标准库也会自动收进来**：插件由 `importlib` 运行时动态加载，`main.py` 从不
> `import plugins`，PyInstaller 的静态分析看不到它们用到的模块。第三方库（httpx 等）后端也在用、
> 已被收集，但**只有插件用**的标准库（`imaplib` / `smtplib` / `email` / `zipfile` / `xml` / `html`）
> 不会进包——症状是 exe 里一执行对应操作就 `ModuleNotFoundError: No module named 'imaplib'`，
> 构建期无警告、开发机也复现不出（开发机是完整解释器）。`CredentialVault.spec` 会扫一遍
> `plugins/**/*.py` 的顶层 import，把其中的标准库名补进 `hiddenimports`，新增插件自动生效。
> **第三方库同理要显式收**：邮箱走代理靠 `PySocks`（`import socks`），spec 里单独
> `collect_all("socks")`，缺包即中止构建——不然 exe 里给邮箱设代理会报「缺少 PySocks」。
> Telegram 校验靠 `telethon`，其代理支持另需 **`python-socks`**：注意 telethon 的轮子里
> **没有** `socks` 这个 extra，`telethon[socks]` 装不来它，故 `requirements.txt` 里把它单独列了
> 一行；缺它 Telethon 会**静默忽略代理直连**（插件据此拒绝联网校验），spec 里同样
> `collect_all` + 缺包即中止。
>
> **`--frontend` 的前端预检**：`node_modules` **存在不等于能用**。从 Linux/macOS 直接拷过来的
> `node_modules` 里，`.bin/vite` 是 POSIX 脚本/符号链接，而 Windows 要的是 `.bin/vite.cmd`——
> 症状是 `npm run build` 报 `'vite' is not recognized as an internal or external command`（看着很像
> 代码问题，其实是依赖问题）；用 `npm install --omit=dev` 装过也一样（`vite` 在 `devDependencies` 里
> 被跳过）。`build.py` 会在跑构建前先查 `.bin` 里有没有可执行的 `vite`，命中就直接给出
> 「删掉 `frontend/node_modules` 重新 `npm install`」的修法，不再直接抛出 npm 的英文报错。
>
> Windows 运行前提：需已安装 WebView2 Runtime（Windows 10/11 通常已内置）。

### 打包源码归档

需要把源码本身打成压缩包（分发、备份、迁移到 Windows 构建机）时，用：

```bash
python scripts/pack_source.py     # 生成/覆盖 release/credential-vault-src.zip
```

- 打包范围 = **当前工作树源码**（git 已追踪文件 + 未被 `.gitignore` 忽略的新增文件），
  改完源码直接跑即可，无需先 commit。
- 自动排除 `.venv` / `node_modules` / `data/`（含真实密钥）/ `release` / `dist` /
  `build` / `frontend/dist` / `__pycache__`，不含符号链接。
- 内置安全校验：一旦发现 `real_keys` / `*.db` / `master.key` 会被打包即中止。
- 纯 Python `zipfile` 实现，跨平台（Windows 上无需系统 `zip` 命令）。


## 项目结构

```
API-Key-Checker/
├── main.py                  # 应用入口
├── requirements.txt
├── CredentialVault.spec     # PyInstaller 打包规格
├── backend/
│   ├── api.py               # pywebview JS-API 桥接层
│   ├── crypto.py            # AES-256-GCM 加密/解密
│   ├── database.py          # SQLite 数据访问层
│   ├── backup.py            # 备份 / 恢复（导出单个 JSON，可选加密；导入自动识别）
│   ├── paths.py             # 冻结感知路径解析（打包/开发统一）
│   ├── icon_ink.py          # 插件 logo 深色主题可见度判定（该不该垫一层浅色底）
│   ├── plugin_base.py       # 插件接口定义
│   ├── plugin_manager.py    # 插件目录扫描与自动加载
│   ├── plugin_sdk.py        # 框架暴露给插件的唯一接口层（VerifyResult / make_client / guarded …）
│   ├── redact.py            # 输出净化（把插件结果 / 日志里回显的敏感字段值替换成 ***）
│   ├── preset_store.py      # 类别/插件默认设置（存 + 继承 + 注入）
│   ├── proxy.py             # 代理工具（规范化/校验/写入 VAULT_PROXY）
│   ├── proxy_store.py       # 代理注册表与「按凭据解析代理」
│   ├── proxy_test.py        # 代理连通性诊断（直连/代理对照探测）
│   ├── single_instance.py   # 单实例闸门（互斥体 + 把已有窗口拉到前台）
│   ├── scheduler.py         # 定时验证调度器
│   ├── tasks.py             # 后台任务注册表（长操作的状态 / 进度 / 取消）
│   ├── verify_options.py    # 批量验证运行参数（并发 / 超时）的唯一事实来源
│   ├── idle_lock.py         # 空闲自动锁定看门狗（无操作一段时间后清除内存密钥）
│   ├── vault_proxy_ctx.py   # 按调用生效的代理上下文（插件经 SDK 的 proxy() 读取）
│   ├── logger.py            # 日志配置（轮转文件 + 级别可调 + 关键词搜索）
│   ├── tray.py              # 系统托盘
│   └── mcp_server.py        # MCP Server（SSE）
├── assets/                  # 应用图标资源（icon.ico / icon.png，由脚本生成）
├── scripts/
│   ├── build.py             # 可执行文件打包脚本（输出到 release/）
│   ├── check_exe_icon.py    # 校验 exe 是否真的嵌入了 assets/icon.ico（区分「没嵌」与「图标缓存」）
│   ├── make_icon.py         # 生成应用图标（ICO/PNG/favicon）
│   └── pack_source.py       # 源码归档脚本（生成 release/credential-vault-src.zip）
├── plugins/                 # 插件源码（打包时作为外部目录，不进 exe）
│   ├── llm/                 # LLM 类别：18 个厂商子插件；llm_common.py 为类别特有辅助
│   ├── api/                 # API 服务类别：firecrawl / tavily / metaso / coze / dify / fofa /
│   │                        #   google_api_credentials / gateway
│   └── dns/ dev/ im/ cloud/ exchange/ mail/   # 另 6 个类别共 25 个子插件
├── frontend/                # Vue 3 前端
│   ├── src/
│   │   ├── views/
│   │   ├── components/
│   │   └── stores/
│   ├── public/              # 静态资源（favicon.svg / favicon.png）
│   └── vite.config.js
├── release/                 # 打包产物（gitignored，构建时生成）
└── docs/                    # 项目文档
    ├── ARCHITECTURE.md
    ├── PLUGIN_GUIDE.md
    └── MCP_API.md
```

## 文档

- [架构设计](docs/ARCHITECTURE.md)
- [前端设计](docs/FRONTEND.md)
- [插件开发指南](docs/PLUGIN_GUIDE.md)
- [MCP 接口规范](docs/MCP_API.md)
- [数据库设计](docs/DATABASE.md)

## 许可

MIT
