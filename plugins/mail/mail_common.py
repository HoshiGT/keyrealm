"""邮箱类别共享模块：仅提供多服务通用的工具。

plugins/mail/ 下的每个子插件（smtp……）都是独立插件，通过
`from mail_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

本模块**只放通用工具**，不包含任何具体服务的验证逻辑：
  - VerifyResult   插件验证结果数据结构（独立定义，不依赖 backend）
  - log            统一 [mail] 前缀的 stderr 日志
  - guarded        插件边界兜底装饰器（未预期异常 → error 结果，只暴露异常类型名）
  - _as_text / _as_bool / _as_port  前端字段取值兼容助手（text/checkbox/number）

具体服务（如 SMTP）的连接、登录与结果判定全部下沉到各自的子插件目录
（plugins/mail/<service>/plugin.py）。新增邮箱类插件时照抄 smtp 子插件即可，
**无需改动本模块**。

本模块不涉及 httpx / 代理，也完全不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


def log(message: str) -> None:
    """统一 [mail] 前缀的插件日志（写 stderr，避免污染 stdout）。"""
    print(f"[mail] {message}", file=sys.stderr)


def guarded(fn):
    """插件边界兜底装饰器。

    把 fn 中逃逸的未预期异常转成 error 结果，且只暴露异常类型名（不回显异常正文，
    避免把 host / 授权码等敏感信息带进消息）。适用于 verify / validate 等入口。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"{fn.__name__} 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")

    return wrapper


# ── 取值辅助（兼容前端 checkbox→bool、number→int、text→str） ──

def _as_text(value: Any) -> str:
    """统一转字符串并去除首尾空白（避免密码/账号带入多余空格）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    """checkbox 可能为 bool / 数字 / 字符串，逐一兼容。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_port(value: Any, default: int) -> int:
    """端口兼容 int / 数字字符串；非法或越界时回落默认值。"""
    if value is None or value == "":
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default
