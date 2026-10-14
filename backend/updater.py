"""程序内更新（从源码编译）—— 检查 / 构建 / 数据库迁移 / 重启的**唯一实现**。

设计要点
--------
- **唯一实现，双入口复用**：本模块既被应用内（`backend/api.py` 转发）调用，也被
  `scripts/{check_version,build_update,db_update,restart}.py` 四个独立 CLI 复用（它们只做
  argparse → 转发到这里）。逻辑只写一份。
- **放 `backend/` 而不是 `scripts/`**：冻结的 exe 只打包 `main.py` 的 import 图（`backend/` +
  `frontend/dist` + `assets`），`scripts/` 不进 exe —— 应用内的 `check_update()` 必须在 exe 内
  可调用。本模块只依赖标准库级内部模块（`paths`/`version`/`database`/`single_instance`），
  因此**任何 Python 3.11+ 都能 import 它**（含 venv 尚未创建时），四个脚本据此复用。
- **只读检查不碰工作区**：`check_update()` 用 `git clone --depth 1` 到临时目录读远端版本，
  用完即删；真正前进工作区的是 `fetch_latest()`（`git fetch --depth 1` + `merge --ff-only`）。
- **交棒顺序不可颠倒**：应用内 `start_update()` 必须**先把更新脚本起成脱离进程、再退出应用**，
  否则窗口一销毁就没机会再 spawn。更新脚本第一件事是 `wait_for_app_exit()`（借单实例锁判断
  应用是否已退）。

两档更新方式**不同**：
- **正式版（formal）**：对比 **GitHub Release 的最新正式版标签**（`vX.Y.Z`），有新版就**下载对应
  平台的预编译包**（`Keyrealm-<版本>-<平台>.zip`）解压替换当前程序、迁移数据库、重启——**不编译**。
- **测试版（test）**：跟到 `main` 最新提交（版本号 **或** commit 任一不同即算有更新），
  **从源码构建**（本机没有源码会先拉一份到程序目录），再迁移、重启。
"""
from __future__ import annotations

import ast
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import database as db
from . import logger
from . import single_instance
from . import version
from .paths import app_dir, is_frozen

_log = logger.get_logger(__name__)

REPO_URL = "https://github.com/YZBRH/keyrealm.git"
REPO_SLUG = "YZBRH/keyrealm"
DEFAULT_BRANCH = "main"
CLONE_DEPTH = 1
CHANNELS = ("formal", "test")

SETTING_CHANNEL = "update_channel"

# 备份里默认跳过的 data/ 子目录（logs 可能很大；installed_plugins 是旧缓存）
_BACKUP_SKIP_DIRS = {"logs", "installed_plugins"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remote_url() -> str:
    """远端地址。环境变量 ``KEYREALM_UPDATE_REMOTE`` 可覆盖（离线自测指向本地 bare 仓库）。"""
    return os.environ.get("KEYREALM_UPDATE_REMOTE", "").strip() or REPO_URL


# 明确表示「强制直连、不要走全局默认代理」的取值
_NO_PROXY = ("direct", "none", "off", "0", "false")

# 会被代理覆盖的标准环境变量（小写 + 大写都设，覆盖 git / pip / npm 各家读法）
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


# 更新代理的三种模式（与应用内「使用代理」同一套语义）
MODE_GLOBAL = "global"    # 跟随「网络与代理」的全局默认代理
MODE_DIRECT = "direct"    # 强制直连
MODE_PROXY = "proxy"      # 指定「代理列表」里的某个代理
_MODES = (MODE_GLOBAL, MODE_DIRECT, MODE_PROXY)

SETTING_PROXY_MODE = "update_proxy_mode"
SETTING_PROXY_ID = "update_proxy_id"


def get_update_proxy() -> dict:
    """当前「更新代理」选择：``{"mode", "proxy_id", "resolved"}``。

    ``resolved`` 是解析后的实际代理地址（``""`` = 直连），供界面显示。
    """
    mode = (db.get_setting(SETTING_PROXY_MODE, "") or "").strip().lower()
    if mode not in _MODES:
        mode = MODE_GLOBAL
    pid = (db.get_setting(SETTING_PROXY_ID, "") or "").strip() if mode == MODE_PROXY else ""
    return {"mode": mode, "proxy_id": pid, "resolved": update_proxy() or ""}


def set_update_proxy(mode: str, proxy_id: str = "") -> dict:
    """设置更新走哪个代理。``mode`` ∈ global / direct / proxy（== 指定 ``proxy_id``）。"""
    mode = (mode or "").strip().lower()
    if mode not in _MODES:
        return {"ok": False, "message": f"无效的更新代理模式：{mode or '(空)'}"}
    pid = (proxy_id or "").strip()
    if mode == MODE_PROXY:
        try:
            from . import proxy_store
            if not proxy_store.get_proxy_url(pid):
                return {"ok": False, "message": "指定的代理不存在"}
        except Exception:  # noqa: BLE001
            return {"ok": False, "message": "读取代理列表失败"}
    db.set_setting(SETTING_PROXY_MODE, mode)
    db.set_setting(SETTING_PROXY_ID, pid if mode == MODE_PROXY else "")
    return {"ok": True, **get_update_proxy()}


def update_proxy() -> Optional[str]:
    """本次更新（git / pip / npm）实际走哪个代理，返回代理 URL 或 ``None``（直连）。

    优先级：
    1. 环境变量 ``KEYREALM_UPDATE_PROXY``（可设为 ``direct`` 强制直连）——CLI / 排障逃生口；
    2. 应用内「更新代理」选择（``settings.update_proxy_mode`` / ``update_proxy_id``）：
       ``global`` 跟随「网络与代理」的全局默认代理、``direct`` 直连、``proxy`` 指定某个代理。

    引用了已被删掉的代理 → 回落到直连（``proxy_store.get_proxy_url`` 返回 None）。
    """
    raw = os.environ.get("KEYREALM_UPDATE_PROXY", "").strip()
    if raw:
        return None if raw.lower() in _NO_PROXY else raw
    mode = (db.get_setting(SETTING_PROXY_MODE, "") or "").strip().lower()
    if mode not in _MODES:
        mode = MODE_GLOBAL
    try:
        from . import proxy_store
        if mode == MODE_DIRECT:
            return None
        if mode == MODE_PROXY:
            return proxy_store.get_proxy_url(db.get_setting(SETTING_PROXY_ID, "")) or None
        return proxy_store.global_url() or None
    except Exception:  # noqa: BLE001 - 取不到就当直连
        return None


def proxy_env() -> dict:
    """把本次更新该用的代理铺成一组环境变量，交给所有子进程（git / pip / npm）。

    **直连时把各变量置空串**，以压掉从 shell 继承来的同名变量——否则用户 shell 里若有
    ``http_proxy``，选了「直连」也仍会走它。``no_proxy`` 保证 localhost 不走代理。
    """
    url = update_proxy()
    env = {key: (url or "") for key in _PROXY_ENV_KEYS}
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


def _child_env(extra: Optional[dict] = None) -> dict:
    """子进程环境 = 当前环境 + 更新代理（+ 额外项）。"""
    return {**os.environ, **proxy_env(), **(extra or {})}


def _short(text: Any, limit: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


# ── 路径 ──────────────────────────────────────────────────

def repo_root(root: Optional[Any] = None) -> Path:
    return Path(root) if root else app_dir()


def is_source_checkout(root: Optional[Any] = None) -> bool:
    """当前是不是 git 源码仓库（仓库根有 ``.git``）。不是的话「从源码编译」无从谈起。"""
    return (repo_root(root) / ".git").is_dir()


def _data_dir(root: Optional[Any] = None) -> Path:
    return repo_root(root) / "data"


def backups_dir(root: Optional[Any] = None) -> Path:
    return repo_root(root) / "backups"


def status_file(root: Optional[Any] = None) -> Path:
    return _data_dir(root) / ".update-status.json"


def request_file(root: Optional[Any] = None) -> Path:
    return _data_dir(root) / ".update-request.json"


def migrations_dirs(root: Optional[Any] = None) -> list:
    """迁移脚本可能来自两处：源码树 ``scripts/migrations/`` 与下载包解出的 ``.update-meta/migrations/``。"""
    root = repo_root(root)
    return [root / "scripts" / "migrations", root / ".update-meta" / "migrations"]


def _exe_name() -> str:
    return "Keyrealm.exe" if os.name == "nt" else "Keyrealm"


# ── 版本比较 ──────────────────────────────────────────────

def _parse_ver(value: Any) -> list[int]:
    parts: list[int] = []
    for chunk in str(value or "").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return parts or [0]


def _semver_gt(a: Any, b: Any) -> bool:
    """``a`` 是否严格大于 ``b``（三段数值比较，不引入 packaging 依赖）。"""
    pa, pb = _parse_ver(a), _parse_ver(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _read_version_module(text: str) -> tuple[Optional[str], Optional[str]]:
    """从 ``version.py`` 源码里取 ``APP_VERSION`` / ``SCHEMA_VERSION``（用 ast，不 eval）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, None
    app = schema = None

    def _const(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "APP_VERSION":
                app = _const(node.value)
            elif target.id == "SCHEMA_VERSION":
                schema = _const(node.value)
    return app, schema


# ── 平台 / 正式版 Release 资产 ────────────────────────────

def platform_pkg() -> str:
    """当前平台的发布包后缀，与 CI 产物命名一致：``windows-x64`` / ``macos-arm64`` / ``linux-x64``。"""
    if os.name == "nt":
        osname = "windows"
    elif sys.platform == "darwin":
        osname = "macos"
    else:
        osname = "linux"
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        arch = machine or "unknown"
    return f"{osname}-{arch}"


def release_asset_name(ver: str) -> str:
    return f"Keyrealm-{ver}-{platform_pkg()}.zip"


def release_asset_url(ver: str) -> str:
    """按 CI 的命名约定拼出 Release 资产下载地址（无需 GitHub API，也就没有速率限制）。"""
    tag = ver if ver.startswith("v") else f"v{ver}"
    return f"https://github.com/{REPO_SLUG}/releases/download/{tag}/{release_asset_name(ver)}"


def latest_release_tag(*, timeout: float = 30) -> dict:
    """取远端**最新正式版标签**（``vX.Y.Z``），走 ``git ls-remote --tags``（受更新代理管辖）。

    返回 ``{"ok": True, "version": "1.3.0", "tag": "v1.3.0"}``；无标签或失败时 ``ok=False``。
    """
    if not git_available():
        return {"ok": False, "error": "no_git", "message": "本机未安装 git，无法检查更新"}
    url = _remote_url()
    try:
        r = _git(["ls-remote", "--tags", url], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "访问远端仓库超时" + _NET_HINT}
    if r.returncode != 0:
        return {"ok": False, "error": "network",
                "message": f"无法访问远端仓库：{_short(r.stderr) or '未知错误'}" + _NET_HINT}
    versions: list[str] = []
    for line in (r.stdout or "").splitlines():
        ref = line.split("\t", 1)[-1].strip()
        if ref.endswith("^{}"):        # 标注 tag 的解引用附带行，跳过
            continue
        name = ref.rsplit("/", 1)[-1]
        if name.startswith("v") and _parse_ver(name[1:]) != [0]:
            versions.append(name[1:])
    if not versions:
        return {"ok": False, "error": "no_release",
                "message": "远端还没有正式版标签（vX.Y.Z）——可能尚未发过 Release。"}
    best = versions[0]
    for v in versions[1:]:
        if _semver_gt(v, best):
            best = v
    return {"ok": True, "version": best, "tag": f"v{best}"}


def download_release_asset(url: str, dest: Path) -> dict:
    """下载 Release 资产到 ``dest``（走更新代理，先写 .part 再原子替换）。"""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "缺少 httpx，无法下载预编译包"}
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(proxy=update_proxy(),
                          timeout=httpx.Timeout(connect=15.0, read=180.0, write=180.0, pool=15.0),
                          follow_redirects=True, trust_env=False) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return {"ok": False, "message": f"下载失败（HTTP {resp.status_code}）：{url}"}
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"下载失败：{type(exc).__name__}"}
    return {"ok": True, "bytes": dest.stat().st_size}


def update_meta_dir(root: Optional[Any] = None) -> Path:
    """下载包里的更新元数据目录（``version.json`` + ``migrations/``）。"""
    return repo_root(root) / ".update-meta"


def read_release_meta(directory: Path) -> dict:
    """读 ``.update-meta/version.json``（``{app_version, schema_version, commit}``）；不存在返回 {}。"""
    return _read_json(Path(directory) / "version.json") or {}


def extract_release_zip(zip_path: Path, app_dir: Path) -> dict:
    """把发布包解压覆盖到程序目录：**剥离顶层目录**、**跳过 ``data/``**（绝不覆盖用户数据）。"""
    app_dir = Path(app_dir)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            tops = {i.filename.split("/", 1)[0] for i in infos if "/" in i.filename}
            strip = (next(iter(tops)) + "/") if len(tops) == 1 else ""
            for info in infos:
                name = info.filename
                if strip and name.startswith(strip):
                    name = name[len(strip):]
                if not name or name.endswith("/"):
                    continue
                if name.split("/", 1)[0] == "data":     # 用户数据目录原样保留
                    continue
                target = app_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                mode = (info.external_attr >> 16) & 0o777   # 还原可执行位（macOS/Linux）
                if mode:
                    try:
                        os.chmod(target, mode)
                    except OSError:
                        pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"解压失败：{exc}"}
    return {"ok": True}


def source_meta(root: Optional[Any] = None) -> dict:
    """读程序目录里**源码**的 ``backend/version.py``（拉取后的新值），返回 app/schema 版本。"""
    vf = repo_root(root) / "backend" / "version.py"
    if not vf.is_file():
        return {}
    app, schema = _read_version_module(vf.read_text(encoding="utf-8", errors="replace"))
    return {"app_version": app or "", "schema_version": schema or ""}


# ── 观测：本地 / 远端 ─────────────────────────────────────

def local_info(root: Optional[Any] = None) -> dict:
    root = repo_root(root)
    return {
        "root": str(root),
        "version": version.APP_VERSION,
        "commit": version.build_id(),
        "schema_version": version.SCHEMA_VERSION,
        "frozen": is_frozen(),
        "source_ok": is_source_checkout(root),
    }


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(args: list[str], cwd: Optional[Path] = None, timeout: float = 60) -> subprocess.CompletedProcess:
    # 代理**必须经 ``-c http.proxy`` 传**：git 的 ``http.proxy`` 配置优先级**高于**
    # ``http_proxy``/``all_proxy`` 环境变量——用户全局 git 配置里若已有 ``http.proxy``，
    # 光设环境变量会被它盖掉，更新代理就形同虚设（本机就踩到过：全局配了 127.0.0.1:10809）。
    # 故这里统一注入命令级 ``-c http.proxy=<本次解析出的代理或空>``，让 git 严格按我们的解析走
    # （空值 = 强制直连，同样压过全局配置）。环境变量仍保留，供 pip / npm 等其它子进程使用。
    proxy = update_proxy()
    return subprocess.run(
        ["git", "-c", f"http.proxy={proxy or ''}", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        env=_child_env(),
    )


_NET_HINT = "；若本机访问 GitHub 需要代理，请在「设置 → 网络与代理」配置默认代理（更新会复用该代理）后重试"


def remote_info(*, branch: str = DEFAULT_BRANCH, root: Optional[Any] = None,
                timeout: float = 30) -> dict:
    """只读地取远端 main 的版本号与 commit（**不改工作区**）。

    ``git ls-remote`` 拿 commit（极小请求）；需要版本号时再 ``clone --depth 1`` 到临时目录读
    ``backend/version.py``，读完删掉。任何失败都收敛成 ``{"ok": False, ...}``，不抛异常。
    """
    if not git_available():
        return {"ok": False, "error": "no_git",
                "message": "本机未安装 git，无法检查更新（请先安装 git）"}
    url = _remote_url()
    try:
        ls = _git(["ls-remote", url, f"refs/heads/{branch}"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "访问远端仓库超时（网络不可用？）" + _NET_HINT}
    if ls.returncode != 0:
        return {"ok": False, "error": "network",
                "message": f"无法访问远端仓库：{_short(ls.stderr) or '未知错误'}" + _NET_HINT}
    line = (ls.stdout or "").strip().splitlines()
    commit = line[0].split()[0][:7] if line and line[0].split() else ""
    if not commit:
        return {"ok": False, "error": "branch", "message": f"远端没有分支 {branch}"}

    tmp = Path(tempfile.mkdtemp(prefix="keyrealm-update-"))
    try:
        cl = _git(["clone", "--depth", str(CLONE_DEPTH), "--single-branch",
                   "--branch", branch, url, str(tmp)], timeout=max(timeout, 90))
        if cl.returncode != 0:
            return {"ok": False, "error": "clone",
                    "message": f"浅克隆失败：{_short(cl.stderr) or '未知错误'}" + _NET_HINT}
        vfile = tmp / "backend" / "version.py"
        text = vfile.read_text(encoding="utf-8", errors="replace") if vfile.is_file() else ""
        app, schema = _read_version_module(text)
        return {"ok": True, "version": app or "", "schema_version": schema or "",
                "commit": commit, "via": "git"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "拉取远端源码超时" + _NET_HINT}
    except Exception as exc:  # noqa: BLE001 - 检查边界，绝不抛
        return {"ok": False, "error": "error", "message": f"检查更新失败：{type(exc).__name__}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare(kind: str, local: dict, remote: dict) -> dict:
    """按档位判定是否有更新。"""
    lv = str(local.get("version") or "")
    rv = str(remote.get("version") or "")
    if kind == "test":
        lc = str(local.get("commit") or "").replace("-dirty", "")
        rc = str(remote.get("commit") or "").replace("-dirty", "")
        ver_diff = rv != lv
        com_diff = bool(lc and rc and lc != rc)
        if ver_diff or com_diff:
            reason = "远端版本号不同" if ver_diff else "远端有新提交"
            return {"has_update": True, "reason": reason}
        return {"has_update": False, "reason": "版本号与提交均与远端一致"}
    # formal
    if _semver_gt(rv, lv):
        return {"has_update": True, "reason": f"远端版本更高（{lv} → {rv}）"}
    return {"has_update": False, "reason": "已是最新正式版"}


def check_update(kind: str = "formal", *, branch: str = DEFAULT_BRANCH,
                 timeout: float = 30, root: Optional[Any] = None) -> dict:
    kind = kind if kind in CHANNELS else "formal"
    local = local_info(root)
    dbst = db_status(root=root)
    proxy = update_proxy() or ""

    if kind == "formal":
        # 正式版比对「GitHub Release 的最新正式版标签」——更新时直接下载预编译包，不编译。
        rel = latest_release_tag(timeout=timeout)
        if not rel.get("ok"):
            return {"ok": False, "kind": kind, "current": local,
                    "error": rel.get("error"), "message": rel.get("message"), "proxy": proxy}
        ver = rel["version"]
        has = _semver_gt(ver, local["version"])
        reason = (f"远端正式版更高（{local['version']} → {ver}）" if has else "已是最新正式版")
        return {
            "ok": True, "kind": kind, "has_update": has, "reason": reason,
            "current": local,
            "latest": {"version": ver, "commit": "", "schema_version": ""},
            "asset": release_asset_name(ver), "platform": platform_pkg(),
            "db_change": bool(dbst.get("needs_migration")), "db_chain": dbst.get("chain", []),
            "source_ok": local["source_ok"], "proxy": proxy,
        }

    # 测试版：沿用源码口径（跟 main 的版本号 + commit），更新时从源码构建。
    remote = remote_info(branch=branch, root=root, timeout=timeout)
    if not remote.get("ok"):
        return {"ok": False, "kind": kind, "current": local,
                "error": remote.get("error"), "message": remote.get("message"), "proxy": proxy}
    cmp = compare(kind, local, remote)
    return {
        "ok": True, "kind": kind,
        "has_update": cmp["has_update"], "reason": cmp["reason"],
        "current": local,
        "latest": {"version": remote.get("version", ""), "commit": remote.get("commit", ""),
                   "schema_version": remote.get("schema_version", "")},
        "db_change": bool(dbst.get("needs_migration")), "db_chain": dbst.get("chain", []),
        "source_ok": local["source_ok"], "proxy": proxy,
    }


# ── 通道（正式/测试）持久化 ───────────────────────────────

def get_channel() -> str:
    value = (db.get_setting(SETTING_CHANNEL, "") or "").strip().lower()
    return value if value in CHANNELS else "formal"


def set_channel(channel: str) -> dict:
    value = (channel or "").strip().lower()
    if value not in CHANNELS:
        return {"ok": False, "message": f"无效的更新渠道：{channel}"}
    db.set_setting(SETTING_CHANNEL, value)
    return {"ok": True, "channel": value}


# ── 数据库逐级迁移 ────────────────────────────────────────

def _load_module(path: Path):
    import importlib.util

    name = f"keyrealm_migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载迁移脚本：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover_migration_chain(frm: str, to: str, *, root: Optional[Any] = None,
                             dirs: Optional[list] = None) -> list[tuple]:
    """串出「frm → to」的连续迁移链，返回 ``[(from, to, path), …]``。

    只提供「低一级 → 高一级」的脚本；本函数把它们串起来，**绝不跨级**。断链时返回已找到的
    前缀（调用方据此报「缺哪一跳」）。脚本来自 ``migrations_dirs()``（源码树 + 下载包）。
    """
    directories = [Path(d) for d in (dirs if dirs is not None else migrations_dirs(root))]
    by_from: dict[str, tuple[str, Path]] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("m_*__to_*.py")):
            try:
                mod = _load_module(path)
            except Exception as exc:  # noqa: BLE001 - 坏脚本跳过，别拦整条链
                _log.warning("迁移脚本无法加载 %s：%s", path.name, exc)
                continue
            f = getattr(mod, "FROM_VERSION", "")
            t = getattr(mod, "TO_VERSION", "")
            if f and t:
                by_from.setdefault(f, (t, path))
    steps: list[tuple] = []
    cur = frm
    seen: set[str] = set()
    while cur != to and cur not in seen and len(steps) < 200:
        seen.add(cur)
        nxt = by_from.get(cur)
        if not nxt:
            break
        t, path = nxt
        steps.append((cur, t, path))
        cur = t
    return steps


def db_status(*, root: Optional[Any] = None) -> dict:
    """磁盘上的 DB schema_version 与源码里的 SCHEMA_VERSION 之差。"""
    root = repo_root(root)
    app_schema = version.SCHEMA_VERSION
    db_path = root / "data" / "credentials.db"
    if not db_path.is_file():
        return {"ok": True, "disk": "", "app": app_schema,
                "needs_migration": False, "chain": []}
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        conn.close()
        disk = str(row[0]) if row and row[0] else ""
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "app": app_schema, "message": f"无法读取数据库版本：{exc}"}
    needs = bool(disk) and _semver_gt(app_schema, disk)
    chain: list[str] = []
    if needs:
        chain = [f"{a}→{b}" for a, b, _ in discover_migration_chain(disk, app_schema, root=root)]
    return {"ok": True, "disk": disk, "app": app_schema,
            "needs_migration": needs, "chain": chain}


def _snapshot_db(conn: sqlite3.Connection, db_path: Path, tag: str) -> Path:
    """SQLite 一致性快照（WAL 下也安全）。"""
    dest = Path(f"{db_path}.bak-{tag}-{time.strftime('%Y%m%d-%H%M%S')}")
    with sqlite3.connect(str(dest)) as back:
        conn.backup(back)
    return dest


def run_migrations(*, root: Optional[Any] = None, apply: bool = False,
                   guard: bool = True, backup: bool = True,
                   to: Optional[str] = None, dirs: Optional[list] = None) -> dict:
    """逐级应用数据库迁移。``apply=False``（默认）只演练。

    ``to`` 为目标结构版本，缺省取**当前运行代码**的 ``SCHEMA_VERSION``；正式版更新时目标来自
    下载包内的 ``version.json``、测试版来自拉取后的新源码，故由调用方显式传入。
    ``guard=True`` 时先抢单实例锁（拒绝在应用运行时迁移）；``apply_update`` 已先占锁，传 ``False``。
    """
    root = repo_root(root)
    st = db_status(root=root)
    if not st.get("ok"):
        return {"ok": False, "message": st.get("message", "无法读取数据库版本")}
    frm = st["disk"]
    to = (to or "").strip() or st["app"]
    if not frm:
        return {"ok": True, "steps": [], "message": "数据库尚未创建，无需迁移"}
    if frm == to:
        return {"ok": True, "steps": [], "message": f"数据库结构已是最新（{to}）"}
    if not _semver_gt(to, frm):
        return {"ok": True, "steps": [],
                "message": f"磁盘版本 {frm} 不低于目标版本 {to}，无需迁移"}

    steps = discover_migration_chain(frm, to, root=root, dirs=dirs)
    labels = [f"{a}→{b}" for a, b, _ in steps]
    if not steps or steps[-1][1] != to:
        reached = steps[-1][1] if steps else frm
        return {"ok": False, "steps": labels,
                "message": f"迁移链断档：缺少从 {reached} 升级的脚本（需 m_{reached}__to_*.py，"
                           f"放在 scripts/migrations/ 或 .update-meta/migrations/）"}
    if not apply:
        return {"ok": True, "dry_run": True, "steps": labels,
                "message": "演练：将逐级迁移 " + " → ".join([frm] + [b for _, b, _ in steps])}

    if guard and not single_instance.acquire():
        return {"ok": False, "message": "检测到应用正在运行，请先完全退出再更新数据库"}

    db_path = root / "data" / "credentials.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    done: list[str] = []
    try:
        for f, t, path in steps:
            if backup:
                _snapshot_db(conn, db_path, f)
            mod = _load_module(path)
            with conn:  # 事务：异常自动回滚该跳
                mod.apply(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES "
                    "('schema_version', ?, ?)", (t, _now()))
            done.append(f"{f}→{t}")
            _log.info("数据库迁移完成一跳：%s → %s", f, t)
    except Exception as exc:  # noqa: BLE001 - 迁移失败要如实报，不吞
        _log.exception("数据库迁移失败")
        return {"ok": False, "steps": done, "db_path": str(db_path),
                "message": f"迁移失败（已完成 {len(done)} 跳）：{exc}"}
    finally:
        conn.close()
    return {"ok": True, "applied": done, "db_path": str(db_path),
            "message": f"数据库已逐级迁移：{frm} → {to}"}


# ── 备份 / 恢复 ───────────────────────────────────────────

def _zip_add_tree(zf: zipfile.ZipFile, src: Path, arc_prefix: str,
                  skip_names: Optional[set] = None) -> None:
    if not src.is_dir():
        return
    skip = skip_names or set()
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in skip for part in rel.parts):
            continue
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            zf.write(path, f"{arc_prefix}/{rel.as_posix()}")


def make_backup(*, root: Optional[Any] = None, kind: str = "formal", keep: int = 1) -> dict:
    """把可执行文件 + ``plugins/`` + ``data/`` 打包成 zip；**至多保留 ``keep`` 份**。"""
    root = repo_root(root)
    bdir = backups_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)
    # 「只留一份」：写新备份前先删旧的
    for old in sorted(bdir.glob("keyrealm-backup-*.zip")):
        try:
            old.unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"keyrealm-backup-{kind}-{version.APP_VERSION}-{stamp}.zip"
    members: list[str] = []
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            exe = root / _exe_name()
            if exe.is_file():
                zf.write(exe, exe.name)
                members.append(exe.name)
            _zip_add_tree(zf, root / "plugins", "plugins")
            # data/：DB 单独做一致性快照，其余直接收（跳过 logs / 旧缓存）
            _zip_add_tree(zf, root / "data", "data",
                          skip_names=set(_BACKUP_SKIP_DIRS) | {"credentials.db",
                                                               "credentials.db-wal",
                                                               "credentials.db-shm"})
            db_path = root / "data" / "credentials.db"
            if db_path.is_file():
                snap = db_path.with_name("credentials.db.snapshot")
                try:
                    with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(snap)) as dst:
                        src.backup(dst)
                    zf.write(snap, "data/credentials.db")
                    members.append("data/credentials.db")
                finally:
                    snap.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"备份失败：{exc}"}
    try:
        os.chmod(dest, 0o600)  # 含真实密钥，限权（Windows 上基本无效，忽略）
    except OSError:
        pass
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
            "members": members,
            "message": "备份含真实密钥（凭据库 / 密钥文件），请妥善保管"}


def restore_backup(zip_path: Path, *, root: Optional[Any] = None) -> dict:
    """把备份 zip 解压覆盖回仓库根（``--rollback`` 用）。"""
    root = repo_root(root)
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return {"ok": False, "message": f"备份文件不存在：{zip_path}"}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"恢复失败：{exc}"}
    return {"ok": True, "message": f"已从备份恢复：{zip_path}"}


def latest_backup(*, root: Optional[Any] = None) -> Optional[Path]:
    items = sorted(backups_dir(root).glob("keyrealm-backup-*.zip"))
    return items[-1] if items else None


# ── 取源码（会改工作区）─────────────────────────────────────

def fetch_latest(*, root: Optional[Any] = None, branch: str = DEFAULT_BRANCH,
                 force: bool = False) -> dict:
    """``git fetch --depth 1`` + ``merge --ff-only``：把工作区快进到远端。

    脏工作区默认**拒绝**（避免覆盖用户未提交的改动）；``force=True`` 时先 ``git stash`` 再合。
    """
    root = repo_root(root)
    if not git_available():
        return {"ok": False, "message": "未安装 git"}
    if not is_source_checkout(root):
        return {"ok": False, "message": "当前不是 git 源码仓库（未检测到 .git）"}
    dirty = _git(["status", "--porcelain"], cwd=root).stdout.strip()
    stashed = False
    if dirty:
        if not force:
            return {"ok": False,
                    "message": "工作区有未提交改动，为避免覆盖已中止。请先提交或 stash，"
                               "或加 --force（会先 git stash 再合并）"}
        st = _git(["stash", "push", "-u", "-m", "keyrealm-update-autostash"], cwd=root)
        stashed = st.returncode == 0
    try:
        fe = _git(["fetch", "--depth", str(CLONE_DEPTH), "origin", branch],
                  cwd=root, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "git fetch 超时（网络不可用？）"}
    if fe.returncode != 0:
        return {"ok": False, "message": f"git fetch 失败：{_short(fe.stderr)}"}
    mg = _git(["merge", "--ff-only", "FETCH_HEAD"], cwd=root)
    if mg.returncode != 0:
        return {"ok": False,
                "message": f"无法快进合并（本地已分叉，未做任何改动）：{_short(mg.stderr)}"}
    return {"ok": True, "stashed": stashed, "message": "源码已快进到远端最新"}


# ── 构建 ──────────────────────────────────────────────────

def run_build(*, root: Optional[Any] = None, frontend: bool = True,
              extra_args: Optional[list] = None, python: Optional[str] = None) -> dict:
    """跑 ``scripts/build.py``。``python`` 指定用哪个解释器（默认 ``sys.executable``）。

    冻结 exe 里的自更新要**用 venv 的解释器**构建（``sys.executable`` 是 exe，跑不了脚本），
    故由调用方传入 ``ensure_venv()`` 的路径。
    """
    root = repo_root(root)
    build = root / "scripts" / "build.py"
    if not build.is_file():
        return {"ok": False, "message": "未找到 scripts/build.py（本机没有源码，无法从源码构建）"}
    py = python or sys.executable
    cmd = [py, str(build)]
    if frontend:
        cmd.append("--frontend")
    cmd += list(extra_args or [])
    _log.info("开始构建：%s", " ".join(cmd))
    try:
        # 构建里的 npm install / npm run build 也要走更新代理
        rc = subprocess.run(cmd, cwd=str(root), env=_child_env()).returncode
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"构建无法启动：{exc}"}
    if rc != 0:
        return {"ok": False, "returncode": rc, "message": f"构建失败（退出码 {rc}）"}
    return {"ok": True, "returncode": 0, "message": "构建完成"}


def ensure_deps(*, root: Optional[Any] = None, extra: tuple = ("pyinstaller",),
                python: Optional[str] = None) -> dict:
    """确保当前解释器里装好 ``requirements.txt`` 与构建额外依赖（``pyinstaller``）。

    **走更新代理**（见 ``proxy_env``）——否则本机访问 PyPI 需要代理时，引导脚本里的 pip 会失败。
    已满足依赖时 pip 只会快速校验、几乎无耗时。
    """
    root = repo_root(root)
    env = _child_env()
    py = python or sys.executable
    commands: list[list[str]] = []
    req = root / "requirements.txt"
    if req.is_file():
        commands.append([py, "-m", "pip", "install", "-q", "-r", str(req)])
    commands.append([py, "-m", "pip", "install", "-q", *extra])
    for cmd in commands:
        try:
            r = subprocess.run(cmd, cwd=str(root), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"依赖安装失败：{exc}"}
        if r.returncode != 0:
            return {"ok": False, "message": f"依赖安装失败：{_short(r.stderr or r.stdout)}"}
    return {"ok": True, "message": "依赖已就绪"}


def _venv_python(root: Optional[Any] = None) -> Path:
    """项目下虚拟环境的解释器路径（Linux `.venv/bin/python` / Windows `.venv\\Scripts\\python.exe`）。"""
    base = repo_root(root) / ".venv"
    return base / ("Scripts" / "python.exe" if os.name == "nt" else "bin" / "python")


def ensure_venv(root: Optional[Any] = None) -> dict:
    """确保项目下有可用的虚拟环境，返回其解释器路径（没有就现建一个，离线可完成）。"""
    root = repo_root(root)
    venv_py = _venv_python(root)
    if venv_py.is_file():
        return {"ok": True, "python": str(venv_py)}
    sys_py = shutil.which("python3") or shutil.which("python") or sys.executable
    try:
        r = subprocess.run([sys_py, "-m", "venv", str(root / ".venv")], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"创建虚拟环境失败：{exc}"}
    if r.returncode != 0 or not venv_py.is_file():
        return {"ok": False, "message": f"创建虚拟环境失败：{_short(r.stderr) or '未知错误'}"}
    return {"ok": True, "python": str(venv_py)}


def ensure_source(*, root: Optional[Any] = None, branch: str = DEFAULT_BRANCH,
                  force: bool = False) -> dict:
    """确保程序目录是一份（最新的）源码：是仓库就快进，不是就就地初始化并检出。

    供**测试版**（从源码构建）用——从 Releases 装的二进制版本机没有源码，这里把它转成源码部署
    （``git init`` + ``fetch`` + ``checkout -f``）；``data/``、可执行文件等未跟踪文件原样保留。
    """
    root = repo_root(root)
    if not git_available():
        return {"ok": False, "message": "未安装 git，无法从源码构建"}
    if is_source_checkout(root):
        return fetch_latest(root=root, branch=branch, force=force)
    url = _remote_url()
    try:
        _git(["init"], cwd=root)
        _git(["remote", "remove", "origin"], cwd=root)
        if _git(["remote", "add", "origin", url], cwd=root).returncode != 0:
            return {"ok": False, "message": "配置 git 远端失败"}
        fe = _git(["fetch", "--depth", str(CLONE_DEPTH), "origin", branch], cwd=root, timeout=180)
        if fe.returncode != 0:
            return {"ok": False, "message": f"拉取源码失败：{_short(fe.stderr)}"}
        co = _git(["checkout", "-f", "-B", branch, "FETCH_HEAD"], cwd=root)
        if co.returncode != 0:
            return {"ok": False, "message": f"检出源码失败：{_short(co.stderr)}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "拉取源码超时（网络不可用？）"}
    return {"ok": True, "message": "已把源码拉取到程序目录（转为源码部署）"}


# ── 进程：脱离 / 等待退出 / 重启 ───────────────────────────

def spawn_detached(argv: list[str], *, cwd: Path, log: Optional[Path] = None,
                   env: Optional[dict] = None) -> int:
    """起一个**脱离父进程**的子进程，返回 pid。

    Windows：``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``（切断控制台继承）；
    POSIX：``start_new_session=True``（新会话，脱离控制终端）。
    """
    full_env = {**os.environ, **(env or {})}
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    sink = None
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        sink = open(log, "ab")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=sink or subprocess.DEVNULL,
            stderr=sink or subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        if sink is not None:
            sink.close()
    return proc.pid


def wait_for_app_exit(*, root: Optional[Any] = None, timeout: float = 90,
                      poll: float = 1.0) -> bool:
    """等下应用完全退出，并**顺手占住单实例锁**（更新期间不让应用启动）。

    判据就是单实例锁：应用还在跑 → `acquire()` 拿不到 → 继续等；拿到了说明应用已退。
    调用方（`apply_update`）在重启新应用前必须 `single_instance.release()`。
    """
    deadline = time.monotonic() + timeout
    while True:
        if single_instance.acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def relaunch_app(*, root: Optional[Any] = None, prefer: str = "auto") -> dict:
    """重启应用（脱离当前更新进程）。

    ``prefer``：``"exe"`` 跑预编译二进制（正式版更新后）、``"source"`` 跑 ``python main.py``
    （测试版在源码部署下）、``"auto"`` 按当前形态（冻结→exe，源码→main.py）。
    """
    root = repo_root(root)
    exe = root / _exe_name()
    main_py = root / "main.py"
    if prefer == "auto":
        prefer = "source" if (not is_frozen() and main_py.is_file()) else "exe"
    if prefer == "source" and main_py.is_file():
        argv = [sys.executable, str(main_py)]
    elif exe.is_file():
        argv = [str(exe)]
    elif main_py.is_file():
        argv = [sys.executable, str(main_py)]
    else:
        return {"ok": False, "message": "找不到可启动的程序（既无可执行文件，也无 main.py）"}
    log = root / "data" / "logs" / f"relaunch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    try:
        pid = spawn_detached(argv, cwd=root, log=log)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"重启失败：{exc}"}
    return {"ok": True, "pid": pid, "argv": argv}


# ── 状态 / 请求文件（跨进程、跨重启）────────────────────────

def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _write_status(root: Optional[Any], payload: dict) -> None:
    path = status_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": _now()}
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log.warning("写更新状态失败：%s", exc)


def read_status(root: Optional[Any] = None) -> dict:
    return _read_json(status_file(root)) or {}


def read_request(root: Optional[Any] = None) -> dict:
    return _read_json(request_file(root)) or {}


# ── 编排：更新主流程 ──────────────────────────────────────

def _relaunch_spec(root: Path) -> dict:
    exe = root / _exe_name()
    if is_frozen():
        return {"frozen": True, "exe": str(exe), "argv": [], "cwd": str(root)}
    return {"frozen": False, "exe": sys.executable, "argv": [str(root / "main.py")], "cwd": str(root)}


def _stages_for(kind: str) -> list:
    if kind == "formal":
        return ["backup", "download", "migrate", "restart"]
    return ["backup", "source", "deps", "build", "migrate", "restart"]


def apply_update(kind: str = "formal", *, root: Optional[Any] = None, frontend: bool = True,
                 do_backup: bool = True, do_migrate: bool = True, do_restart: bool = True,
                 force: bool = False, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
    """更新主流程：等应用退出 → 按渠道执行 → 迁移 → 重启（全程写状态文件）。

    - **正式版**：下载对应平台的预编译包 → 解压替换 → 迁移 → 重启（**不编译**）。
    - **测试版**：获取/拉取源码 → 装依赖 → 构建 → 迁移 → 重启。

    由 ``main.py --self-update``（应用内交棒）或 ``scripts/build_update.py`` 调用。
    """
    root = repo_root(root)
    req = read_request(root)
    kind = (req.get("kind") or kind) if req else kind
    kind = kind if kind in CHANNELS else "formal"
    stages = _stages_for(kind)

    if dry_run:
        log(f"演练（{kind}）：将依次执行 " + " → ".join(stages))
        return {"ok": True, "dry_run": True, "kind": kind, "stages": stages}

    _write_status(root, {"phase": "waiting", "ok": None, "kind": kind, "stages": stages,
                         "message": "等待应用退出…"})
    if not wait_for_app_exit(root=root):
        _write_status(root, {"phase": "failed", "ok": False, "kind": kind, "stages": stages,
                             "message": "等待应用退出超时"})
        return {"ok": False, "message": "等待应用退出超时"}

    frm = version.APP_VERSION
    try:
        if kind == "formal":
            result = _apply_formal(root, do_backup=do_backup, do_migrate=do_migrate, log=log)
        else:
            result = _apply_test(root, frontend=frontend, do_backup=do_backup,
                                 do_migrate=do_migrate, force=force, log=log)
    except Exception as exc:  # noqa: BLE001 - 编排边界兜底，如实报错
        _log.exception("更新流程异常")
        result = {"ok": False, "message": f"更新异常：{type(exc).__name__}"}

    prefer = result.pop("prefer_restart", "auto")
    if do_restart and result.get("ok"):
        _write_status(root, {"phase": "restart", "ok": None, "kind": kind, "stages": stages,
                             "message": "重启程序…"})
        single_instance.release()          # 必须先放锁，否则新应用会把自己当第二实例退出
        rl = relaunch_app(root=root, prefer=prefer)
        if rl.get("ok"):
            log(f"已重启（pid={rl.get('pid')}）")
        else:
            result = {"ok": False, "message": rl.get("message", "重启失败")}
    else:
        single_instance.release()
    return _finish(root, kind, frm, result, stages)


def _apply_formal(root: Path, *, do_backup: bool, do_migrate: bool,
                  log: Callable[[str], None]) -> dict:
    """正式版：查最新正式版 → 备份 → 下载预编译包 → 解压替换 → 迁移。重启偏好 ``exe``。"""
    def stage(name: str, message: str) -> None:
        _write_status(root, {"phase": name, "ok": None, "kind": "formal",
                             "stages": _stages_for("formal"), "message": message})

    stage("download", "查询最新正式版…")
    rel = latest_release_tag()
    if not rel.get("ok"):
        return {"ok": False, "message": rel.get("message", "查询最新正式版失败")}
    ver = rel["version"]
    if not _semver_gt(ver, version.APP_VERSION):
        return {"ok": True, "to": version.APP_VERSION, "prefer_restart": "exe",
                "message": f"已是最新正式版（{version.APP_VERSION}）"}

    if do_backup:
        stage("backup", "备份旧产物…")
        bk = make_backup(root=root, kind="formal")
        if not bk.get("ok"):
            return {"ok": False, "message": bk.get("message", "备份失败")}
        log(f"已备份 → {bk['path']}（{bk['message']}）")

    stage("download", f"下载预编译包 {release_asset_name(ver)}…")
    tmp = backups_dir(root) / f"_asset-{ver}.zip"
    dl = download_release_asset(release_asset_url(ver), tmp)
    if not dl.get("ok"):
        return {"ok": False, "message": f"{dl.get('message')}（该平台可能尚未发布对应包）"}
    log(f"已下载（{dl.get('bytes', 0) // 1024} KB）")

    ex = extract_release_zip(tmp, root)
    tmp.unlink(missing_ok=True)
    if not ex.get("ok"):
        return {"ok": False, "message": ex.get("message")}

    if do_migrate:
        stage("migrate", "迁移数据库结构…")
        to_schema = str(read_release_meta(update_meta_dir(root)).get("schema_version") or "")
        if to_schema:
            mg = run_migrations(root=root, apply=True, guard=False, to=to_schema)
            if not mg.get("ok"):
                return {"ok": False, "message": mg.get("message")}
            log(mg.get("message", "数据库已就绪"))
    return {"ok": True, "to": ver, "prefer_restart": "exe"}


def _apply_test(root: Path, *, frontend: bool, do_backup: bool, do_migrate: bool,
                force: bool, log: Callable[[str], None]) -> dict:
    """测试版：备份 → 获取源码 → 建/补环境 → 构建 → 迁移。重启偏好按当前形态。"""
    def stage(name: str, message: str) -> None:
        _write_status(root, {"phase": name, "ok": None, "kind": "test",
                             "stages": _stages_for("test"), "message": message})

    if do_backup:
        stage("backup", "备份旧产物…")
        bk = make_backup(root=root, kind="test")
        if not bk.get("ok"):
            return {"ok": False, "message": bk.get("message", "备份失败")}
        log(f"已备份 → {bk['path']}")

    stage("source", "获取最新源码…")
    src = ensure_source(root=root, force=force)
    if not src.get("ok"):
        return {"ok": False, "message": src.get("message")}
    log(src.get("message", "源码已就绪"))

    stage("deps", "准备构建环境（虚拟环境 + 依赖）…")
    vn = ensure_venv(root=root)
    if not vn.get("ok"):
        return {"ok": False, "message": vn.get("message")}
    py = vn["python"]
    dp = ensure_deps(root=root, python=py)
    if not dp.get("ok"):
        return {"ok": False, "message": dp.get("message")}
    log(dp.get("message", "依赖已就绪"))

    stage("build", "从源码构建（scripts/build.py --frontend）…")
    bd = run_build(root=root, frontend=frontend, python=py)
    if not bd.get("ok"):
        return {"ok": False, "message": bd.get("message")}
    log(bd.get("message", "构建完成"))

    meta = source_meta(root)                       # 新源码的版本（迁移目标）
    if do_migrate and meta.get("schema_version"):
        stage("migrate", "迁移数据库结构…")
        mg = run_migrations(root=root, apply=True, guard=False, to=meta["schema_version"])
        if not mg.get("ok"):
            return {"ok": False, "message": mg.get("message")}
        log(mg.get("message", "数据库已就绪"))

    return {"ok": True, "to": meta.get("app_version") or version.APP_VERSION,
            "prefer_restart": "exe" if is_frozen() else "source"}


def _finish(root: Path, kind: str, frm: str, result: dict, stages: list) -> dict:
    ok = bool(result.get("ok"))
    to = result.get("to") or version.APP_VERSION
    msg = "更新完成" if ok else result.get("message", "更新失败")
    if ok:
        msg = f"已更新到 {to}"
    _write_status(root, {
        "phase": "done" if ok else "failed", "ok": ok, "kind": kind,
        "from": frm, "to": to, "message": msg, "stages": stages,
        "finished_at": _now(),
    })
    _log.info("更新流程结束：ok=%s %s", ok, msg)
    return {**result, "ok": ok, "message": msg}


# ── 应用内入口：交棒 ──────────────────────────────────────

def _self_update_argv(root: Path, kind: str) -> list[str]:
    """「自更新」进程的命令：冻结 exe 用 ``--self-update``（exe 内跑 updater），
    源码运行用 ``python main.py --self-update``。两者都**不建窗口**，跑完即退。"""
    if is_frozen():
        return [sys.executable, "--self-update", kind]
    return [sys.executable, str(root / "main.py"), "--self-update", kind]


def _spawn_updater(root: Path, kind: str, terminal: str) -> int:
    """把「自更新」起成脱离进程。Windows 走独立 CMD 窗口；POSIX 走新会话并记日志。"""
    argv = _self_update_argv(root, kind)
    env = proxy_env()          # 把更新代理注入子进程（git / pip / npm 据此出网）
    if os.name == "nt":
        # cmd /c start "" cmd /k <命令>：新控制台窗口（满足「打开 CMD 窗口」）
        inner = subprocess.list2cmdline(argv)
        return spawn_detached(["cmd", "/c", "start", "Keyrealm 更新", "cmd", "/k", inner],
                              cwd=root, env=env)
    log = root / "data" / "logs" / f"update-{time.strftime('%Y%m%d-%H%M%S')}.log"
    return spawn_detached(argv, cwd=root, log=log, env=env)


def start_update(kind: str = "formal", *, root: Optional[Any] = None,
                 restart: bool = True, terminal: str = "auto",
                 quit_cb: Optional[Callable[[], None]] = None) -> dict:
    """应用内「立即更新」：写请求文件 → 起脱离进程 → 关程序（交棒）。

    **顺序不可颠倒**：先把自更新进程起起来，确认起来了，再退出应用。
    正式版只需能联网下载预编译包；测试版需要能拿到源码（有 git 即可，本机没源码会自动拉）。
    """
    root = repo_root(root)
    kind = kind if kind in CHANNELS else "formal"
    if kind == "test" and not git_available():
        return {"ok": False, "message": "测试版需要从源码构建：本机未安装 git，无法拉取源码。"}

    req = {"kind": kind, "restart": bool(restart), "terminal": terminal or "auto",
           "requested_at": _now(), "relaunch": _relaunch_spec(root)}
    path = request_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        pid = _spawn_updater(root, kind, req["terminal"])
    except Exception as exc:  # noqa: BLE001 - 起不来就绝不退出应用
        _log.error("启动更新进程失败：%s", exc)
        return {"ok": False, "message": f"无法启动更新进程：{exc}"}

    _log.info("已启动更新进程 pid=%s（渠道=%s）", pid, kind)
    _write_status(root, {"phase": "spawned", "ok": None, "kind": kind,
                         "message": "已启动更新进程，等待应用退出…"})

    if quit_cb is not None:
        # 延迟一点再退出：保证本次 IPC 的返回值能先回到前端
        def _later() -> None:
            time.sleep(0.6)
            try:
                quit_cb()
            except Exception as e:  # noqa: BLE001
                _log.warning("退出应用失败：%s", e)

        threading.Thread(target=_later, daemon=True).start()

    hint = "将下载预编译包并替换当前程序" if kind == "formal" else "将从源码重新构建"
    return {"ok": True, "pid": pid,
            "message": f"已开始更新：程序将关闭，{hint}，完成后自动重启。"}
