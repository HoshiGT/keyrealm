"""交易所类别共享模块：仅提供通用工具。

plugins/exchange/ 下的每个子插件（ht、okx、binance）都是独立插件，
各服务把自身的校验与验证逻辑完整实现放在自己的 plugin.py 中；
本模块只提供跨服务复用的通用工具（结果结构、代理、HTTP 客户端、
响应解析与异常兜底）。新增服务插件照抄同级目录即可，无需修改本文件。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import httpx


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


# 请求超时（秒）
TIMEOUT = 20.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


def log(message: str) -> None:
    print(f"[exchange] {message}", file=sys.stderr)


def proxy() -> Optional[str]:
    """验证代理：优先 VAULT_PROXY（应用设置写入），其次标准代理环境变量。"""
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def make_client(follow_redirects: bool = True) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=follow_redirects, proxy=proxy())


def safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def guarded(fn):
    """装饰器：把 verify 抛出的未预期异常统一转成 error 结果。

    仅暴露异常类型名，不泄漏异常消息（可能包含凭据或响应正文）。
    """

    def wrapper(data):
        try:
            return fn(data or {})
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
        except httpx.HTTPError as exc:
            log(f"verify 网络异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")

    wrapper.__name__ = getattr(fn, "__name__", "verify")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


__all__ = [
    "VerifyResult",
    "TIMEOUT",
    "log",
    "proxy",
    "make_client",
    "safe_json",
    "body_snippet",
    "guarded",
]
