"""
Credential Vault — 入口
pywebview 主窗口 + 系统托盘 + MCP Server + 调度器
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

# 确保项目根目录在 sys.path
BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import webview  # pywebview

from backend import api, database as db, idle_lock, logger, mcp_server, plugin_manager, proxy as proxy_config, scheduler, single_instance, tray, verify_options
from backend import vault_proxy_ctx
from backend.paths import app_dir, is_frozen, resource_dir

# 把「按调用生效的代理上下文」以**固定别名**注册进 sys.modules：插件侧的 proxy() 靠这个名字
# 懒查它（插件不允许 import backend，见 docs/PLUGIN_GUIDE.md §3）。必须在任何插件调用之前完成，
# 所以放在模块级而不是 main() 里。
sys.modules.setdefault("vault_proxy_ctx", vault_proxy_ctx)


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
    def list_credentials(self, group_id=None, credential_type=None, status=None,
                         search=None, credential_types=None):
        return api.list_credentials(group_id, credential_type, status, search, credential_types)

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

    def batch_verify(self, scope: str = "all", group_id=None, credential_type=None,
                     credential_ids=None, concurrency=None, timeout=None):
        return api.batch_verify(scope, group_id, credential_type, credential_ids,
                                concurrency, timeout)

    def get_batch_options(self):
        return api.get_batch_options()

    def check_duplicate_credential(self, credential_type: str, data: dict):
        return api.check_duplicate_credential(credential_type, data)

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

    def set_plugin_enabled(self, credential_type: str, enabled: bool):
        return api.set_plugin_enabled(credential_type, enabled)

    def list_credential_actions(self, credential_id: int):
        return api.list_credential_actions(credential_id)

    def credential_capabilities(self, credential_id: int):
        return api.credential_capabilities(credential_id)

    def pick_save_path(self, default_name: str = "", file_types=None):
        return api.pick_save_path(default_name, file_types)

    def run_credential_action(self, credential_id: int, action_id: str, params=None):
        return api.run_credential_action(credential_id, action_id, params)

    def list_credential_models(self, credential_id: int):
        return api.list_credential_models(credential_id)

    def list_draft_models(self, credential_type: str, data=None):
        return api.list_draft_models(credential_type, data)

    def stream_credential_action(self, credential_id: int, action_id: str, params=None, title: str = ""):
        return api.stream_credential_action(credential_id, action_id, params, title)

    # ── 后台任务 ──
    def list_tasks(self):
        return api.list_tasks()

    def cancel_task(self, task_id: str):
        return api.cancel_task(task_id)

    def clear_finished_tasks(self):
        return api.clear_finished_tasks()

    # ── MCP ───────────────────────────────────────────────
    def get_mcp_status(self):
        return api.get_mcp_status()

    def start_mcp(self):
        return api.start_mcp()

    def stop_mcp(self):
        return api.stop_mcp()

    def get_mcp_options(self):
        return api.get_mcp_options()

    def set_mcp_options(self, host: str, port):
        return api.set_mcp_options(host, port)

    def list_mcp_permissions(self):
        return api.list_mcp_permissions()

    def set_mcp_permission(self, tool_name: str, enabled: bool):
        return api.set_mcp_permission(tool_name, enabled)

    def get_mcp_token(self):
        return api.get_mcp_token()

    def rotate_mcp_token(self):
        return api.rotate_mcp_token()

    # ── Schedules ─────────────────────────────────────────
    def list_schedules(self):
        return api.list_schedules()

    def create_schedule(self, name: str, trigger_type: str, trigger_args: dict,
                        target_scope: str = "all", target_id=None, credential_ids=None):
        return api.create_schedule(name, trigger_type, trigger_args, target_scope, target_id,
                                   credential_ids)

    def update_schedule(self, schedule_id: int, **kwargs):
        return api.update_schedule(schedule_id, **kwargs)

    def delete_schedule(self, schedule_id: int):
        return api.delete_schedule(schedule_id)

    # ── Tray ──────────────────────────────────────────────
    def minimize_to_tray(self):
        return api.minimize_to_tray()

    def get_tray_status(self):
        return api.get_tray_status_api()

    # ── 关闭窗口行为 ──────────────────────────────────────
    def get_close_action(self):
        return api.get_close_action()

    def set_close_action(self, action: str):
        return api.set_close_action(action)

    def confirm_close_action(self, action: str, remember: bool = False):
        return api.confirm_close_action(action, remember)

    # ── 空闲自动锁定 ──────────────────────────────────────
    def get_auto_lock(self):
        return api.get_auto_lock()

    def set_auto_lock(self, minutes):
        return api.set_auto_lock(minutes)

    def touch_activity(self):
        return api.touch_activity()

    # ── 验证代理 ──────────────────────────────────────────
    # ── 验证代理（注册表） ─────────────────────────────────
    def list_proxies(self):
        return api.list_proxies()

    def create_proxy(self, name: str, url: str):
        return api.create_proxy(name, url)

    def update_proxy(self, proxy_id: int, name=None, url=None):
        return api.update_proxy(proxy_id, name, url)

    def delete_proxy(self, proxy_id: int):
        return api.delete_proxy(proxy_id)

    def set_global_proxy(self, proxy_id=None):
        return api.set_global_proxy(proxy_id)

    def test_proxy_id(self, proxy_id=None):
        return api.test_proxy_id(proxy_id)

    # ── 类别 / 插件默认设置 ────────────────────────────────
    def get_credential_defaults(self, credential_type: str):
        return api.get_credential_defaults(credential_type)

    def get_scope_options(self, scope: str, scope_key: str, credential_type: str = ""):
        return api.get_scope_options(scope, scope_key, credential_type)

    def set_scope_options(self, scope: str, scope_key: str, values: dict, credential_type: str = ""):
        return api.set_scope_options(scope, scope_key, values, credential_type)

    def clear_scope_options(self, scope: str, scope_key: str):
        return api.clear_scope_options(scope, scope_key)

    # ── 备份 / 恢复 ───────────────────────────────────────
    def export_backup(self, scope: str = "all", encrypted: bool = True, credential_ids=None):
        return api.export_backup(scope, encrypted, credential_ids)

    def import_backup(self):
        return api.import_backup()

    # ── Logging ───────────────────────────────────────────
    def get_log_config(self):
        return api.get_log_config()

    def set_log_level(self, level: str):
        return api.set_log_level(level)

    def get_logs(self, limit: int = 200, level: str = "", search: str = ""):
        return api.get_logs(limit, level, search)

    def clear_logs(self):
        return api.clear_logs()


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


def _shutdown_services():
    """停止后台服务并标记为正在退出（供"直接退出"路径共用的清理）。"""
    global _quitting
    logger.get_logger("main").info("用户请求退出应用")
    _quitting = True
    scheduler.stop_scheduler()
    idle_lock.stop()
    mcp_server.stop_server()
    tray.stop_tray()


def _hide_to_tray():
    """隐藏窗口到系统托盘（后台运行）。**不弹任何提示**——缩到托盘是常规操作，弹一次反而吵。"""
    if _window:
        _window.hide()


def _on_closing():
    """窗口关闭事件：按用户设置的关闭行为处理（询问/直接退出/后台运行）。"""
    if _quitting:
        return True  # 正在退出，允许关闭

    action = db.get_setting("close_action", "ask").strip().lower()
    if action not in ("ask", "quit", "background"):
        action = "ask"

    if action == "quit":
        _shutdown_services()
        return True  # 允许关闭，应用随之退出
    if action == "background":
        _hide_to_tray()
        return False  # 阻止关闭，驻留托盘
    # ask：先阻止本次关闭，通知前端弹窗让用户选择。
    # 注意：closing 事件运行在 GUI 线程，而 evaluate_js 需要 GUI 线程执行 JS
    # 后才能返回——同步调用会互相等待导致窗口卡死，因此放到守护线程异步推送。
    threading.Thread(
        target=api.push_event, args=("vault:close_confirm",), daemon=True
    ).start()
    return False


def _show_window():
    if _window:
        _window.show()
        _window.restore()


def _lock_vault():
    api.lock_vault()


def _quit_app():
    _shutdown_services()
    if _window:
        _window.destroy()


def main():
    global _window

    # 单实例闸门：已经有实例在跑时，把它的窗口拉到前台然后自己退出，而不是再起一套。
    # 放在最前面（日志都还没配），因为重复启动不该产生任何副作用（连日志都不写）。
    if not single_instance.acquire():
        single_instance.focus_existing()
        sys.exit(0)

    # 尽早初始化日志（默认级别），待数据库就绪后再按用户持久化的级别调整
    logger.setup_logging()
    log = logger.get_logger("main")

    # 初始化数据库
    db.init_db()

    # 应用持久化的日志级别（settings 表）
    persisted = db.get_setting("log_level", "").strip().upper()
    if persisted in logger.LEVELS:
        logger.set_level(persisted)

    # 应用持久化的验证代理（写入 VAULT_PROXY 供插件读取；外部同名变量优先）
    proxy_config.apply_proxy(db.get_setting("verify_proxy", ""))

    # 应用持久化的验证超时（写入 VAULT_TIMEOUT 供插件读取；外部同名变量优先）
    verify_options.apply_timeout()

    log.info("应用启动（frozen=%s，日志级别=%s，数据目录=%s）",
             is_frozen(), logger.get_level(), app_dir())

    # 扫描 plugins/ 装载插件
    plugin_manager.sync_plugins()

    # 注入回调
    api.init_callbacks()

    # 启动调度器
    scheduler.start_scheduler()

    # 启动空闲自动锁定看门狗（是否真的锁由 settings.auto_lock_minutes 决定，默认关闭）
    idle_lock.start()

    # MCP 不随应用自动启动（避免每次启动都监听 8765 端口），
    # 由用户在「MCP 设置」页手动开启；开启后最小化到托盘仍持续运行。

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

    # 注入 window 引用与关闭行为回调到 api 模块
    api.set_window(_window)
    api.set_close_callbacks(quit_cb=_quit_app, hide_cb=_hide_to_tray)

    # 启动托盘
    tray.start_tray(
        show_window_cb=_show_window,
        lock_vault_cb=_lock_vault,
        quit_cb=_quit_app,
        get_status_cb=api.get_tray_status,
    )

    # 绑定关闭事件
    _window.events.closing += _on_closing

    # 启动 pywebview（阻塞主线程）。
    # private_mode=False + 固定 storage_path：持久化 localStorage（列顺序、
    # 目录树状态等用户偏好），否则每次启动 WebView2 使用临时配置全部丢失。
    #
    # 图标：显式传入 .ico。不传时 pywebview 会从 sys.executable 提取 exe 内嵌图标，
    # 但提取到的通常是 16×16 小图，在高 DPI 任务栏/标题栏下模糊甚至回退为默认图标。
    # 图标随包在 resource_dir()/assets/（见 CredentialVault.spec）。
    webview_dir = db.DATA_DIR / "webview"
    webview_dir.mkdir(parents=True, exist_ok=True)
    _icon_file = resource_dir() / "assets" / "icon.ico"
    _icon = str(_icon_file) if _icon_file.is_file() else None
    webview.start(
        debug=False, private_mode=False, storage_path=str(webview_dir), icon=_icon
    )
    log.info("应用退出")


if __name__ == "__main__":
    # 调试/构建校验：打印冻结后解析到的各关键路径后退出（不创建窗口）。
    # 用于确认打包产物中 frontend/dist 随包、data/plugins 落在 exe 同级。
    if "--print-paths" in sys.argv:
        print(f"frozen           : {is_frozen()}")
        print(f"resource_dir     : {resource_dir()}")
        print(f"app_dir          : {app_dir()}")
        print(f"frontend_index   : {_get_frontend_path()}")
        print(f"db_path          : {db.DB_PATH}")
        print(f"plugins_dir      : {plugin_manager.PLUGINS_DIR}")
        sys.exit(0)
    main()
