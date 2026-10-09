"""开机自启动：直接读写**操作系统**的自启条目。

设计取舍
--------
"是否开机自启"的**唯一事实来源是操作系统的自启配置本身**（Windows 注册表 Run 项 /
Linux XDG autostart / macOS LaunchAgent）。若再往 settings 表存一份镜像，用户手动删掉
系统条目后两边就会长期不一致，且没有任何时机能可靠地重新同步。因此本模块**只碰系统侧**，
``is_enabled()`` 每次都现读，``set_enabled()`` 直接增删条目。

各平台落点
----------
- Windows：``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``（仅当前用户，
  无需管理员权限）。
- Linux：``$XDG_CONFIG_HOME/autostart/credential-vault.desktop``（XDG 桌面规范，
  GNOME / KDE / XFCE 等均识别）。
- macOS：``~/Library/LaunchAgents/com.credentialvault.app.plist``。

启动命令由 :func:`launch_argv` 按**冻结与否**推导，不写死任何本机路径。
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .paths import app_dir, is_frozen

_log = logging.getLogger(__name__)

APP_ID = "CredentialVault"                    # Windows 注册表值名 / 通用标识
APP_NAME = "Credential Vault"
LINUX_DESKTOP_FILE = "credential-vault.desktop"
# 写进 .desktop 的归属标记：关闭时据此确认「这个文件是本程序建的」再删。
LINUX_MARKER = "# Managed by Credential Vault"
MACOS_LABEL = "com.credentialvault.app"


class AutostartError(RuntimeError):
    """自启条目读写失败（权限不足、路径不可写、文件非本程序所有等）。"""


def launch_argv() -> list[str]:
    """推导开机启动时执行的命令行。

    - **冻结**（PyInstaller）：直接执行可执行文件本身；
    - **源码运行**：``<python> <项目根>/main.py``。

    纯按运行时状态推导，不含任何写死的本机路径。
    """
    if is_frozen():
        return [sys.executable]
    return [sys.executable, str(app_dir() / "main.py")]


def platform_name() -> str:
    """当前自启实现所对应的平台标识（供界面显示）。"""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ── Windows：HKCU\\...\\Run ──────────────────────────────────────────

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_enabled() -> bool:
    import winreg  # noqa: PLC0415 - 仅 Windows 可用

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_ID)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # 权限异常等一律按「读不到」处理，但留痕
        _log.debug("读取 Windows 自启项失败：%s", exc)
        return False


def _win_set(enabled: bool) -> None:
    import winreg  # noqa: PLC0415

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                # list2cmdline 给出 Windows 命令行所需的标准加引号形式
                winreg.SetValueEx(
                    key, APP_ID, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_argv())
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass  # 本来就没有 → 目标状态已达成
    except OSError as exc:
        raise AutostartError(f"写入注册表失败：{exc}") from exc


# ── Linux：XDG autostart ────────────────────────────────────────────


def _linux_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / LINUX_DESKTOP_FILE


def _desktop_exec(argv: list[str]) -> str:
    """按 Desktop Entry 规范拼 Exec 值。

    该值**不经过 shell**，所以无需 shell 转义；规范要求对 ``"`` `` ` `` ``$`` ``\\``
    加反斜杠转义，含空白或上述字符的参数用双引号包裹。字面量 ``%`` 须写成 ``%%``，
    否则会被当成字段码。
    """
    parts = []
    for arg in argv:
        if arg and not any(c in arg for c in ' \t\n"\'`$\\'):
            parts.append(arg)
            continue
        escaped = (
            arg.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts).replace("%", "%%")


def _linux_enabled() -> bool:
    return _linux_path().is_file()


def _linux_set(enabled: bool) -> None:
    path = _linux_path()
    if not enabled:
        if not path.exists():
            return
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AutostartError(f"读取 {path} 失败：{exc}") from exc
        if LINUX_MARKER not in head:
            # 同名文件但不是本程序写的 → 拒绝删除，交回用户处理
            raise AutostartError(f"{path} 不是本程序创建，拒绝删除；请手动处理")
        try:
            path.unlink()
        except OSError as exc:
            raise AutostartError(f"删除 {path} 失败：{exc}") from exc
        return

    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=本地凭据保险库\n"
        f"Exec={_desktop_exec(launch_argv())}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"{LINUX_MARKER}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise AutostartError(f"写入 {path} 失败：{exc}") from exc


# ── macOS：LaunchAgent ──────────────────────────────────────────────


def _macos_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def _macos_enabled() -> bool:
    return _macos_path().is_file()


def _macos_set(enabled: bool) -> None:
    path = _macos_path()
    if not enabled:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AutostartError(f"删除 {path} 失败：{exc}") from exc
        return
    payload = {
        "Label": MACOS_LABEL,
        "ProgramArguments": launch_argv(),
        "RunAtLoad": True,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(payload))
    except OSError as exc:
        raise AutostartError(f"写入 {path} 失败：{exc}") from exc


# ── 对外接口 ───────────────────────────────────────────────────────

def is_supported() -> bool:
    """当前平台是否实现了自启（三大桌面平台之外一律不支持）。"""
    return sys.platform in ("win32", "darwin") or os.name == "posix"


def is_enabled() -> bool:
    """现读系统侧的自启状态。读取失败按「未启用」处理并留痕。"""
    try:
        if sys.platform == "win32":
            return _win_enabled()
        if sys.platform == "darwin":
            return _macos_enabled()
        return _linux_enabled()
    except Exception as exc:  # noqa: BLE001 - 只读探测不该把界面打挂
        _log.debug("读取自启状态失败：%s", exc)
        return False


def set_enabled(enabled: bool) -> None:
    """开启 / 关闭自启。失败抛 :class:`AutostartError`（消息可直接给用户看）。"""
    if sys.platform == "win32":
        _win_set(bool(enabled))
    elif sys.platform == "darwin":
        _macos_set(bool(enabled))
    else:
        _linux_set(bool(enabled))
    _log.info("开机自启动已%s", "开启" if enabled else "关闭")


def describe_target() -> str:
    """自启条的落点描述（供界面/日志展示，便于用户自行核查）。"""
    if sys.platform == "win32":
        return f"HKEY_CURRENT_USER\\{_WIN_RUN_KEY}"
    if sys.platform == "darwin":
        return str(_macos_path())
    return str(_linux_path())


def status() -> dict:
    """界面用的一份完整状态。"""
    return {
        "supported": is_supported(),
        "enabled": is_enabled(),
        "platform": platform_name(),
        "target": describe_target(),
    }
