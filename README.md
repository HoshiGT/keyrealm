# Credential Vault — 凭据管理与验证平台

一个本地优先的桌面应用，用于集中管理、加密存储和主动验证各类 API 凭据。

## 特性

- **AES-256-GCM 加密存储**：主密码派生密钥，敏感字段全程加密
- **插件化架构**：凭据类型由插件定义，`.zip` 拖入即安装，无需重启
- **MCP 服务**：内置 MCP Server，AI Agent 可直接管理与验证凭据，支持按工具粒度控制权限
- **多模式验证**：保存时自动验证 / 手动验证 / 定时批量验证
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

## 项目结构

```
API-Key-Checker/
├── main.py                  # 应用入口
├── requirements.txt
├── backend/
│   ├── api.py               # pywebview JS-API 桥接层
│   ├── crypto.py            # AES-256-GCM 加密/解密
│   ├── database.py          # SQLite 数据访问层
│   ├── models.py            # 数据模型（Pydantic）
│   ├── plugin_base.py       # 插件接口定义
│   ├── plugin_manager.py    # 插件安装/加载/管理
│   ├── scheduler.py         # 定时验证调度器
│   └── mcp_server.py        # MCP Server（SSE）
├── plugins/                 # 内置插件源码
│   └── llm_api_key/
│       ├── manifest.json
│       └── plugin.py
├── frontend/                # Vue 3 前端
│   ├── src/
│   │   ├── views/
│   │   ├── components/
│   │   └── stores/
│   └── vite.config.js
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
