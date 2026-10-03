# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：Credential Vault 单文件可执行程序。

设计要点
--------
- **onefile**：产出单个可执行文件（Windows 为 CredentialVault.exe）。
- **只打包代码 + frontend/dist**：前端产物作为只读资源随包（运行时从 sys._MEIPASS 取）。
- **不打包 plugins/ 与 data/**：这两者是「用户可改 / 运行时可写」内容，由构建脚本
  以外部目录形式放到 exe 同级（见 scripts/build.py）。这样用户能直接编辑插件、
  数据也不会写进临时解压目录而丢失。

console 开关
------------
默认 GUI 模式（console=False，无黑框）。调试构建可设环境变量 CV_CONSOLE=1 保留控制台，
便于运行 `CredentialVault --print-paths` 校验冻结后的路径解析。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# spec 文件所在目录 = 项目根
ROOT = Path(SPECPATH).resolve()

# 前端构建产物（必须先 npm run build）
FRONTEND_DIST = ROOT / "frontend" / "dist"

_console = os.environ.get("CV_CONSOLE") == "1"

# 应用图标（Windows exe 需 .ico）。文件缺失时传 None，回退 PyInstaller 默认图标。
_ICON_PATH = ROOT / "assets" / "icon.ico"
_icon = str(_ICON_PATH) if _ICON_PATH.exists() else None

datas = []
binaries = []
hiddenimports = []

# 随包只读资源：前端 dist → 包内 frontend/dist
if FRONTEND_DIST.exists():
    datas.append((str(FRONTEND_DIST), "frontend/dist"))

# 逐个收集第三方依赖的数据文件、二进制与隐藏子模块。
# 这些库大量使用动态导入 / 平台特定后端，手工列 hiddenimports 极易漏，交给 collect_all。
for _pkg in [
    "webview",        # pywebview（各平台 WebView 后端）
    "clr",            # pythonnet（Windows 后端，非 Windows 上收集失败会被忽略）
    "clr_loader",
    "pythonnet",
    "pystray",        # 系统托盘（各平台后端）
    "PIL",            # Pillow（托盘图标）
    "apscheduler",    # 定时任务
    "mcp",            # MCP Server
    "uvicorn",        # ASGI 服务器
    "starlette",
    "sse_starlette",  # MCP SSE 传输
    "anyio",
    "sniffio",
    "httpx",          # 插件验证 HTTP 客户端
    "httpcore",
    "h11",
    "certifi",
    "click",
    "cryptography",   # AES-GCM / PBKDF2
    "pydantic",
    "pydantic_core",
    "annotated_types",
]:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        # 平台特有的可选依赖（如 Windows 的 clr）在当前系统不存在，跳过即可
        pass

# 本地 backend 包的全部子模块（api/crypto/database/mcp_server/... ）
try:
    hiddenimports += collect_submodules("backend")
except Exception:
    pass
hiddenimports += [
    "backend",
    "backend.api",
    "backend.crypto",
    "backend.database",
    "backend.mcp_server",
    "backend.models",
    "backend.paths",
    "backend.plugin_base",
    "backend.plugin_manager",
    "backend.scheduler",
    "backend.tray",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # plugins/data/tests/docs 均为外部内容或开发资源，不进包
    excludes=["plugins", "data", "tests", "docs", "pytest", "PyInstaller"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CredentialVault",
    icon=_icon,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
