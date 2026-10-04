# Credential Vault — 凭据管理与验证平台

一个本地优先的桌面应用，用于集中管理、加密存储和主动验证各类 API 凭据。

## 特性

- **AES-256-GCM 加密存储**：主密码派生密钥，敏感字段全程加密
- **插件化架构**：一个服务一个插件，目录放入 `plugins/` 即自动加载，删除即卸载，无需手动管理
- **MCP 服务**：内置 MCP Server，AI Agent 可直接管理与验证凭据，支持按工具粒度控制权限
- **多模式验证**：保存时自动验证 / 手动验证 / 定时批量验证；批量验证可**手动勾选**（支持一键全选）并**并发**执行；有写副作用或无法自动化的类型可声明 `batch_verify: false`，仅允许手动单个验证
- **可读的附加信息**：凭据列表「附加信息」列以「标签 值」展示——LLM 显示余额（plan 类显示使用率），云厂商显示账号信息，无效字段自动隐藏
- **验证代理**：可为所有插件的校验请求配置 http/https/socks5 代理（也可用环境变量 `VAULT_PROXY` 覆盖）
- **分组管理**：平面分组，凭据按项目或用途归类
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
```

构建完成后 `release/` 结构：

```
release/
├── CredentialVault.exe        # 单文件可执行程序（后端代码 + frontend/dist 已内置）
├── credential-vault-src.zip   # 源码归档（由 scripts/pack_source.py 生成，见下）
├── plugins/                   # 插件【唯一事实来源】（不进 exe，可直接编辑，重启生效）
│   ├── llm/                   # LLM 类别目录（category.json + llm_common.py 共享模块）
│   │   ├── openai/            # 每个服务一个独立子插件（manifest.json + plugin.py）
│   │   ├── deepseek/
│   │   └── …                  # 共 18 个 LLM 子插件
│   ├── api/                   # API 服务类别（category.json + api_common.py）
│   │   ├── firecrawl/
│   │   ├── tavily/
│   │   └── coze/
│   └── dns/ dev/ im/ cloud/ exchange/ mail/   # 域名解析 / 开发者平台 / 即时通讯 / 云服务商 / 交易所 / 邮箱
└── data/                      # 首次运行时自动创建（数据库、主密码校验）
```

> 约定：插件只以 `plugins/` 为准。一个服务就是一个独立子插件，放入 `plugins/` 目录
> 即自动加载，删除对应子插件目录即卸载，互不影响，无需手动管理。

打包边界：

| 内容 | 是否进 exe | 说明 |
|------|-----------|------|
| 后端代码、`frontend/dist` | ✅ 内置 | 只读资源，运行时从临时解压目录读取 |
| `plugins/` | ❌ 外部 | 插件唯一事实来源，放 exe 同级；启动时扫描自动加载，删除子插件目录即卸载 |
| `data/` | ❌ 外部 | 运行时可写，放 exe 同级，保证数据持久化 |

`release/` 是自包含、可便携分发的目录，且**不被 git 追踪**（`CredentialVault.spec` 与
`scripts/*.py` 作为构建配置保留在版本库中）。

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
│   ├── paths.py             # 冻结感知路径解析（打包/开发统一）
│   ├── plugin_base.py       # 插件接口定义
│   ├── plugin_manager.py    # 插件目录扫描与自动加载
│   ├── scheduler.py         # 定时验证调度器
│   ├── logger.py            # 日志配置（轮转文件 + 级别可调）
│   ├── tray.py              # 系统托盘
│   └── mcp_server.py        # MCP Server（SSE）
├── assets/                  # 应用图标资源（icon.ico / icon.png，由脚本生成）
├── scripts/
│   ├── build.py             # 可执行文件打包脚本（输出到 release/）
│   ├── make_icon.py         # 生成应用图标（ICO/PNG/favicon）
│   └── pack_source.py       # 源码归档脚本（生成 release/credential-vault-src.zip）
├── plugins/                 # 插件源码（打包时作为外部目录，不进 exe）
│   ├── llm/                 # LLM 类别：18 个厂商子插件 + llm_common.py 共享模块
│   ├── api/                 # API 服务类别：firecrawl / tavily / coze + api_common.py
│   └── dns/ dev/ im/ cloud/ exchange/ mail/   # 另 6 个类别共 19 个子插件
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
- [插件开发指南](docs/PLUGIN_GUIDE.md)
- [MCP 接口规范](docs/MCP_API.md)
- [数据库设计](docs/DATABASE.md)

## 许可

MIT
