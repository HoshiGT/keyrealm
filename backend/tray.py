"""
系统托盘
pystray 后台守护线程，窗口关闭 → 隐藏到托盘
图标颜色：🟢运行+活跃Token 🔵运行空闲 🔴停止/错误 ⚪锁定
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw

_tray_icon: Optional[pystray.Icon] = None
_tray_thread: Optional[threading.Thread] = None
_show_window_cb: Optional[Callable] = None
_lock_vault_cb: Optional[Callable] = None
_quit_cb: Optional[Callable] = None
_get_status_cb: Optional[Callable] = None


def _make_icon_image(color: str) -> Image.Image:
    """生成 64x64 托盘图标：品牌几何（圆角方块 + 保险库门圆环 + 钥匙孔），
    用状态色填充以便一眼判别服务状态。"""
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        "green":  (34, 197, 94, 255),
        "blue":   (59, 130, 246, 255),
        "red":    (239, 68, 68, 255),
        "gray":   (156, 163, 175, 255),
    }
    c = colors.get(color, colors["gray"])

    # 圆角方块底（状态色）
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=c)

    cx = cy = s / 2

    # 保险库门：白色圆环
    ring_r = s * 0.34
    draw.ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        outline=(255, 255, 255, 235),
        width=max(2, int(s * 0.055)),
    )

    # 钥匙孔：上方圆 + 下方梯形柄
    head_r = s * 0.10
    head_cy = cy - s * 0.05
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(255, 255, 255, 255),
    )
    stem_top_y = head_cy + head_r * 0.35
    stem_bot_y = cy + s * 0.19
    top_half = head_r * 0.62
    bot_half = head_r * 1.05
    draw.polygon(
        [
            (cx - top_half, stem_top_y),
            (cx + top_half, stem_top_y),
            (cx + bot_half, stem_bot_y),
            (cx - bot_half, stem_bot_y),
        ],
        fill=(255, 255, 255, 255),
    )
    return img


def _get_icon_color() -> str:
    if _get_status_cb:
        try:
            status = _get_status_cb()
            if status.get("is_locked"):
                return "gray"
            if not status.get("mcp_running"):
                return "red"
            if status.get("active_tokens", 0) > 0:
                return "green"
            return "blue"
        except Exception:
            pass
    return "gray"


def _update_icon():
    if _tray_icon:
        color = _get_icon_color()
        _tray_icon.icon = _make_icon_image(color)
        status = {}
        if _get_status_cb:
            try:
                status = _get_status_cb()
            except Exception:
                pass
        mcp = "运行中" if status.get("mcp_running") else "已停止"
        port = status.get("mcp_port", 8765)
        tokens = status.get("active_tokens", 0)
        locked = "已锁定" if status.get("is_locked") else "已解锁"
        _tray_icon.title = f"Keyrealm | MCP: {mcp} :{port} | Token: {tokens} | {locked}"


def _on_show(icon, item):
    if _show_window_cb:
        _show_window_cb()


def _on_lock(icon, item):
    if _lock_vault_cb:
        _lock_vault_cb()
    _update_icon()


def _on_quit(icon, item):
    if _quit_cb:
        _quit_cb()
    icon.stop()


def _build_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("显示主窗口", _on_show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("锁定保险库", _on_lock),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _on_quit),
    )


def start_tray(
    show_window_cb: Callable,
    lock_vault_cb: Callable,
    quit_cb: Callable,
    get_status_cb: Callable,
):
    """启动托盘（守护线程）"""
    global _tray_icon, _tray_thread
    global _show_window_cb, _lock_vault_cb, _quit_cb, _get_status_cb

    _show_window_cb = show_window_cb
    _lock_vault_cb = lock_vault_cb
    _quit_cb = quit_cb
    _get_status_cb = get_status_cb

    if _tray_icon:
        return

    _tray_icon = pystray.Icon(
        name="Keyrealm",
        icon=_make_icon_image(_get_icon_color()),
        title="Keyrealm",
        menu=_build_menu(),
    )

    _tray_thread = threading.Thread(target=_tray_icon.run, daemon=True)
    _tray_thread.start()


def update_tray():
    """刷新托盘图标状态"""
    _update_icon()


def stop_tray():
    global _tray_icon
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass
        _tray_icon = None
