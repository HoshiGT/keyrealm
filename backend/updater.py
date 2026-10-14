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

两档更新口径（都以 main 为准）：
- 正式版（formal）：远端 `APP_VERSION` **更高**才算有更新；
- 测试版（test）：版本号**或** commit 任一不同即算有更新。
"""
from __future__ import annotations

import ast
import json
import os
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


def migrations_dir(root: Optional[Any] = None) -> Path:
    return repo_root(root) / "scripts" / "migrations"


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
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


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
        return {"ok": False, "error": "network", "message": "访问远端仓库超时（网络不可用？）"}
    if ls.returncode != 0:
        return {"ok": False, "error": "network",
                "message": f"无法访问远端仓库：{_short(ls.stderr) or '未知错误'}"}
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
                    "message": f"浅克隆失败：{_short(cl.stderr) or '未知错误'}"}
        vfile = tmp / "backend" / "version.py"
        text = vfile.read_text(encoding="utf-8", errors="replace") if vfile.is_file() else ""
        app, schema = _read_version_module(text)
        return {"ok": True, "version": app or "", "schema_version": schema or "",
                "commit": commit, "via": "git"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "network", "message": "拉取远端源码超时"}
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
    remote = remote_info(branch=branch, root=root, timeout=timeout)
    if not remote.get("ok"):
        return {"ok": False, "kind": kind, "current": local,
                "error": remote.get("error"), "message": remote.get("message")}
    cmp = compare(kind, local, remote)
    dbst = db_status(root=root)
    return {
        "ok": True, "kind": kind,
        "has_update": cmp["has_update"], "reason": cmp["reason"],
        "current": local,
        "latest": {"version": remote.get("version", ""), "commit": remote.get("commit", ""),
                   "schema_version": remote.get("schema_version", "")},
        "db_change": bool(dbst.get("needs_migration")),
        "db_chain": dbst.get("chain", []),
        "source_ok": local["source_ok"],
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


def discover_migration_chain(frm: str, to: str, *, root: Optional[Any] = None) -> list[tuple]:
    """串出「frm → to」的连续迁移链，返回 ``[(from, to, path), …]``。

    只提供「低一级 → 高一级」的脚本；本函数把它们串起来，**绝不跨级**。断链时返回已找到的
    前缀（调用方据此报「缺哪一跳」）。
    """
    directory = migrations_dir(root)
    if not directory.is_dir():
        return []
    by_from: dict[str, tuple[str, Path]] = {}
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
                   guard: bool = True, backup: bool = True) -> dict:
    """逐级应用数据库迁移。``apply=False``（默认）只演练。

    ``guard=True`` 时先抢单实例锁（拒绝在应用运行时迁移）；``apply_update`` 已先占锁，
    会传 ``guard=False`` 避免自锁。
    """
    root = repo_root(root)
    st = db_status(root=root)
    if not st.get("ok"):
        return {"ok": False, "message": st.get("message", "无法读取数据库版本")}
    frm, to = st["disk"], st["app"]
    if not frm:
        return {"ok": True, "steps": [], "message": "数据库尚未创建，无需迁移"}
    if frm == to:
        return {"ok": True, "steps": [], "message": f"数据库结构已是最新（{to}）"}
    if not _semver_gt(to, frm):
        return {"ok": True, "steps": [],
                "message": f"磁盘版本 {frm} 不低于源码版本 {to}，无需迁移"}

    steps = discover_migration_chain(frm, to, root=root)
    labels = [f"{a}→{b}" for a, b, _ in steps]
    if not steps or steps[-1][1] != to:
        reached = steps[-1][1] if steps else frm
        return {"ok": False, "steps": labels,
                "message": f"迁移链断档：缺少从 {reached} 升级的脚本（需 scripts/migrations/"
                           f"m_{reached}__to_*.py）"}
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
              extra_args: Optional[list] = None) -> dict:
    root = repo_root(root)
    build = root / "scripts" / "build.py"
    if not build.is_file():
        return {"ok": False, "message": "未找到 scripts/build.py"}
    cmd = [sys.executable, str(build)]
    if frontend:
        cmd.append("--frontend")
    cmd += list(extra_args or [])
    _log.info("开始构建：%s", " ".join(cmd))
    try:
        rc = subprocess.run(cmd, cwd=str(root)).returncode
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"构建无法启动：{exc}"}
    if rc != 0:
        return {"ok": False, "returncode": rc, "message": f"构建失败（退出码 {rc}）"}
    return {"ok": True, "returncode": 0, "message": "构建完成"}


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


def relaunch_app(*, root: Optional[Any] = None) -> dict:
    """重启应用（脱离当前更新进程）。优先重启构建出的可执行文件，否则回退 ``python main.py``。"""
    root = repo_root(root)
    exe = root / _exe_name()
    log = root / "data" / "logs" / f"relaunch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    if exe.is_file():
        argv = [str(exe)]
    else:
        argv = [sys.executable, str(root / "main.py")]
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


def apply_update(kind: str = "formal", *, root: Optional[Any] = None, frontend: bool = True,
                 do_backup: bool = True, do_migrate: bool = True, do_restart: bool = True,
                 force: bool = False, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
    """更新主流程：等应用退出 → 备份 → 取源码 → 构建 → 迁移 → 重启（全程写状态文件）。

    由 ``scripts/build_update.py`` 在应用退出后调用。
    """
    root = repo_root(root)
    req = read_request(root)
    kind = (req.get("kind") or kind) if req else kind
    kind = kind if kind in CHANNELS else "formal"
    stages = ["backup", "fetch", "build", "migrate", "restart"]

    if dry_run:
        log("演练：将依次执行 " + " → ".join(stages))
        return {"ok": True, "dry_run": True, "stages": stages}

    _write_status(root, {"phase": "waiting", "ok": None, "kind": kind, "stages": stages,
                         "message": "等待应用退出…"})
    if not wait_for_app_exit(root=root):
        _write_status(root, {"phase": "failed", "ok": False, "kind": kind,
                             "message": "等待应用退出超时", "stages": stages})
        return {"ok": False, "message": "等待应用退出超时"}

    log_capture: list[str] = []

    def say(text: str) -> None:
        log(text)
        log_capture.append(text)

    def stage(name: str, message: str) -> None:
        _write_status(root, {"phase": name, "ok": None, "kind": kind,
                             "stages": stages, "message": message})

    from . import version as _v
    frm = _v.APP_VERSION
    result: dict = {"ok": True, "kind": kind, "from": frm}
    try:
        if do_backup:
            stage("backup", "备份旧产物…")
            bk = make_backup(root=root, kind=kind)
            if not bk.get("ok"):
                result = {"ok": False, "message": bk.get("message", "备份失败")}
                return _finish(root, kind, frm, result, stages)
            say(f"已备份 → {bk['path']}（{bk['message']}）")

        stage("fetch", "拉取最新源码…")
        fr = fetch_latest(root=root, force=force)
        if not fr.get("ok"):
            result = {"ok": False, "message": fr.get("message")}
            return _finish(root, kind, frm, result, stages)
        say(fr.get("message", "源码已更新"))

        stage("build", "重新编译（scripts/build.py --frontend）…")
        bd = run_build(root=root, frontend=frontend)
        if not bd.get("ok"):
            result = {"ok": False, "message": bd.get("message")}
            return _finish(root, kind, frm, result, stages)
        say(bd.get("message", "构建完成"))

        if do_migrate:
            stage("migrate", "检查数据库结构…")
            mg = run_migrations(root=root, apply=True, guard=False)
            if not mg.get("ok"):
                result = {"ok": False, "message": mg.get("message")}
                return _finish(root, kind, frm, result, stages)
            say(mg.get("message", "数据库已就绪"))

        result["to"] = _v.APP_VERSION
    except Exception as exc:  # noqa: BLE001 - 编排边界兜底，如实报错
        _log.exception("更新流程异常")
        result = {"ok": False, "message": f"更新异常：{type(exc).__name__}"}
        return _finish(root, kind, frm, result, stages)
    finally:
        # 到重启之前才释放单实例锁；失败路径也要释放，否则下次起不来
        if not (do_restart and result.get("ok")):
            single_instance.release()

    if do_restart and result.get("ok"):
        stage("restart", "重启程序…")
        single_instance.release()          # 必须先放锁，否则新应用会把自己当第二实例退出
        rl = relaunch_app(root=root)
        say(rl.get("message", f"已重启（pid={rl.get('pid')}）"))
    return _finish(root, kind, frm, result, stages)


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

def _updater_script(root: Path) -> Path:
    return root / "scripts" / ("build_update.bat" if os.name == "nt" else "build_update.sh")


def _spawn_updater(root: Path, terminal: str) -> int:
    """把「构建更新」脚本起成脱离进程。Windows 走独立 CMD 窗口；POSIX 走新会话。"""
    script = _updater_script(root)
    if not script.is_file():
        raise FileNotFoundError(f"未找到更新脚本：{script}")
    if os.name == "nt":
        # cmd /c start "" cmd /k <script>：新控制台窗口（满足「打开 CMD 窗口」）
        argv = ["cmd", "/c", "start", "Keyrealm 更新", "cmd", "/k", str(script)]
        return spawn_detached(argv, cwd=root)
    log = root / "data" / "logs" / f"update-{time.strftime('%Y%m%d-%H%M%S')}.log"
    return spawn_detached(["bash", str(script)], cwd=root, log=log)


def start_update(kind: str = "formal", *, root: Optional[Any] = None,
                 restart: bool = True, terminal: str = "auto",
                 quit_cb: Optional[Callable[[], None]] = None) -> dict:
    """应用内「立即更新」：写请求文件 → 起脱离进程 → 关程序（交棒）。

    **顺序不可颠倒**：先把更新脚本起起来，确认进程起来了，再退出应用。
    """
    root = repo_root(root)
    if not is_source_checkout(root):
        return {"ok": False, "message": "当前不是源码仓库（未检测到 .git），无法从源码重建；"
                                        "请在源码目录中运行，或重新获取分发包。"}
    if is_frozen() and not is_source_checkout(root):
        return {"ok": False, "message": "当前为打包版且无源码，暂不支持程序内更新。"}
    kind = kind if kind in CHANNELS else "formal"

    req = {"kind": kind, "restart": bool(restart), "terminal": terminal or "auto",
           "requested_at": _now(), "relaunch": _relaunch_spec(root)}
    path = request_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        pid = _spawn_updater(root, req["terminal"])
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

    return {"ok": True, "pid": pid,
            "message": "已开始更新：程序将关闭，在终端中备份并重建后自动重启。"}
