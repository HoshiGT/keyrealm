"""
Credential Vault — 入口
pywebview 主窗口 + 系统托盘 + MCP Server + 调度器
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import webview  # pywebview

from backend import api, database as db, mcp_server, plugin_manager, scheduler, tray
from backend.paths import app_dir, is_frozen, resource_dir


class VaultApi:
    """
    pywebview JS-API 暴露类
    所有方法都是同步的（pywebview 在线程池调用）
    """

    # ── Auth ──────────────────────────────────────────────
    def is_initialized(self):
        return api.is_initialized()

    def is_unlocked(self):
        return api.is_unlocked()

    def setup_master_password(self, password: str):
        return api.setup_master_password(password)

    def unlock(self, password: str):
        return api.unlock(password)

    def lock_vault(self):
        return api.lock_vault()

    def change_master_password(self, old_password: str, new_password: str):
        return api.change_master_password(old_password, new_password)

    # ── Credentials ───────────────────────────────────────
    def list_credentials(self, group_id=None, credential_type=None, status=None, search=None):
        return api.list_credentials(group_id, credential_type, status, search)

    def get_credential(self, credential_id: int):
        return api.get_credential(credential_id)

    def create_credential(self, name: str, credential_type: str, data: dict,
                          group_id=None, auto_verify: bool = True):
        return api.create_credential(name, credential_type, data, group_id, auto_verify)

    def update_credential(self, credential_id: int, name=None, group_id=None,
                          data=None, auto_verify: bool = False):
        return api.update_credential(credential_id, name, group_id, data, auto_verify)

    def delete_credential(self, credential_id: int):
        return api.delete_credential(credential_id)

    def verify_credential(self, credential_id: int):
        return api.verify_credential(credential_id)

    def batch_verify(self, scope: str = "all", group_id=None, credential_type=None):
        return api.batch_verify(scope, group_id, credential_type)

    def get_verification_history(self, credential_id: int, limit: int = 20):
        return api.get_verification_history(credential_id, limit)

    # ── Groups ────────────────────────────────────────────
    def list_groups(self):
        return api.list_groups()

    def create_group(self, name: str, description: str = "", color: str = "#409EFF"):
        return api.create_group(name, description, color)

    def update_group(self, group_id: int, name=None, description=None, color=None):
        return api.update_group(group_id, name, description, color)

    def delete_group(self, group_id: int):
        return api.delete_group(group_id)

    # ── Plugins ───────────────────────────────────────────
    def list_plugins(self):
        return api.list_plugins()

    def install_plugin(self, zip_path: str):
        return api.install_plugin(zip_path)

    def uninstall_plugin(self, credential_type: str):
        return api.uninstall_plugin(credential_type)

    def toggle_plugin(self, credential_type: str, enabled: bool):
        return api.toggle_plugin(credential_type, enabled)

    # ── MCP ───────────────────────────────────────────────
    def get_mcp_status(self):
        return api.get_mcp_status()

    def start_mcp(self):
        return api.start_mcp()

    def stop_mcp(self):
        return api.stop_mcp()

    def list_mcp_permissions(self):
        return api.list_mcp_permissions()

    def set_mcp_permission(self, tool_name: str, enabled: bool):
        return api.set_mcp_permission(tool_name, enabled)

    def list_mcp_tokens(self):
        return api.list_mcp_tokens()

    def create_mcp_token(self, name: str, tools_allowed=None, expires_days=None):
        return api.create_mcp_token(name, tools_allowed or [], expires_days)

    def revoke_mcp_token(self, token_id: int):
        return api.revoke_mcp_token(token_id)

    def update_token_scope(self, token_id: int, tools_allowed: list):
        return api.update_token_scope(token_id, tools_allowed)

    # ── Schedules ─────────────────────────────────────────
    def list_schedules(self):
        return api.list_schedules()

    def create_schedule(self, name: str, trigger_type: str, trigger_args: dict,
                        target_scope: str = "all", target_id=None):
        return api.create_schedule(name, trigger_type, trigger_args, target_scope, target_id)

    def update_schedule(self, schedule_id: int, **kwargs):
        return api.update_schedule(schedule_id, **kwargs)

    def delete_schedule(self, schedule_id: int):
        return api.delete_schedule(schedule_id)

    # ── Dashboard ─────────────────────────────────────────
    def get_dashboard_stats(self):
        return api.get_dashboard_stats()

    # ── Tray ──────────────────────────────────────────────
    def minimize_to_tray(self):
        return api.minimize_to_tray()

    def get_tray_status(self):
        return api.get_tray_status_api()


# ── 窗口与托盘 ────────────────────────────────────────────

_window: webview.Window | None = None
_quitting = False


def _get_frontend_path() -> str:
    """返回前端 dist/index.html 路径。

    frontend/dist 随包分发（只读），冻结后从 _MEIPASS 取；开发时从项目根取。
    """
    root = resource_dir()
    dist = root / "frontend" / "dist" / "index.html"
    if dist.exists():
        return str(dist)
    # 开发模式：Vite dev server
    return str(root / "frontend" / "index.html")


def _on_closing():
    """窗口关闭事件 → 隐藏到托盘（不退出）"""
    global _window
    if _quitting:
        return True  # 允许关闭
    if _window:
        _window.hide()
        tray.show_first_minimize_bubble()
    return False  # 阻止关闭


def _show_window():
    global _window
    if _window:
        _window.show()
        _window.restore()


def _lock_vault():
    api.lock_vault()


def _quit_app():
    global _quitting
    _quitting = True
    scheduler.stop_scheduler()
    mcp_server.stop_server()
    tray.stop_tray()
    if _window:
        _window.destroy()


def main():
    global _window

    # 初始化数据库
    db.init_db()

    # 安装内置插件
    plugin_manager.install_builtin_plugins()

    # 注入回调
    api.init_callbacks()

    # 启动调度器
    scheduler.start_scheduler()

    # 自动启动 MCP（如果有活跃 Token）
    if db.count_active_tokens() > 0:
        mcp_server.start_server()

    # 创建窗口
    _window = webview.create_window(
        title="Credential Vault",
        url=_get_frontend_path(),
        js_api=VaultApi(),
        width=1200,
        height=800,
        min_size=(900, 600),
        resizable=True,
        confirm_close=False,
    )

    # 注入 window 引用到 api 模块
    api.set_window(_window)

    # 启动托盘
    tray.start_tray(
        show_window_cb=_show_window,
        lock_vault_cb=_lock_vault,
        quit_cb=_quit_app,
        get_status_cb=api.get_tray_status,
    )

    # 绑定关闭事件
    _window.events.closing += _on_closing

    # 启动 pywebview（阻塞主线程）
    webview.start(debug=False)


if __name__ == "__main__":
    # 调试/构建校验：打印冻结后解析到的各关键路径后退出（不创建窗口）。
    # 用于确认打包产物中 frontend/dist 随包、data/plugins 落在 exe 同级。
    if "--print-paths" in sys.argv:
        print(f"frozen           : {is_frozen()}")
        print(f"resource_dir     : {resource_dir()}")
        print(f"app_dir          : {app_dir()}")
        print(f"frontend_index   : {_get_frontend_path()}")
        print(f"db_path          : {db.DB_PATH}")
        print(f"builtin_plugins  : {plugin_manager.BUILTIN_PLUGINS_DIR}")
        print(f"installed_plugins: {plugin_manager.PLUGINS_DIR}")
        sys.exit(0)
    main()
