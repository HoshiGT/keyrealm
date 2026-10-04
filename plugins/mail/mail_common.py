"""邮箱 类别共享模块：底层通用工具 + 子插件原型接口。

只提供与具体服务无关的最底层能力：

- 结果类型 `VerifyResult`
- 异常兜底 `guarded`、日志 `log`

具体服务（如 SMTP）的连接、登录与结果判定全部下沉到各自
plugins/mail/<服务>/plugin.py（服务之间允许重复，刻意为之）；
新增邮箱类插件照抄 smtp 子插件即可，无需修改本模块。

本类别走标准库 `smtplib`，不涉及 httpx / 代理；本模块也不导入 backend 包，
可脱离主程序单独运行。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional


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
    print(f"[mail] {message}", file=sys.stderr)


def guarded(fn):
    """装饰器：兜底 verify/validate 未预期异常 → error 结果，保证不抛出。

    异常消息只暴露异常类型名，避免把 host / 授权码等敏感信息带进错误提示。
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常（{type(exc).__name__}）")

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


# ── 子插件原型接口（子插件应实现；这里只声明契约，运行时不调用本模块的实现）──

def validate(data: dict) -> list:
    """子插件实现：返回配置错误列表（空列表 = 通过）。"""
    raise NotImplementedError


def verify(data: dict) -> "VerifyResult":
    """子插件实现：连接并验证凭据，返回 VerifyResult。"""
    raise NotImplementedError


def get_display_summary(data: dict) -> str:
    """子插件实现：凭据列表中的简短展示文本。"""
    return ""


def actions(data: dict) -> list:
    """子插件可选实现：声明可用操作；返回 [] 表示无操作。"""
    return []


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """子插件可选实现：执行操作。"""
    return {"ok": False, "error": "未实现"}
