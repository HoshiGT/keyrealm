"""域名解析（dns）类别通用工具。

plugins/dns/ 下的每个子插件（dnspod、duckdns、godaddy…）都是独立插件，
各自在 plugin.py 中实现完整的服务逻辑，只通过 `from dns_common import ...`
复用本模块的通用工具；新增服务插件照抄同级目录即可，无需改动本模块。
删除任一子插件目录不影响其他子插件运行。

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
    """输出带类别前缀的诊断日志到 stderr。"""
    print(f"[dns] {message}", file=sys.stderr)


def proxy() -> Optional[str]:
    """验证代理：优先 VAULT_PROXY（应用设置写入），其次标准代理环境变量。"""
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def make_client(follow_redirects: bool = True) -> httpx.Client:
    """构造统一超时与代理的 HTTP 客户端。"""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=follow_redirects, proxy=proxy())


def safe_json(resp: httpx.Response) -> Optional[dict]:
    """安全解析 JSON 响应；非 JSON 或顶层非对象时返回 None。"""
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(resp: httpx.Response) -> str:
    """压缩空白并截断响应正文，用于错误消息。"""
    try:
        text = resp.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def guarded(fn):
    """验证函数的安全边界：任何异常都转成 error 结果，且只暴露异常类型名。

    作为装饰器直接套在子插件的 verify(data) 上使用。
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
        except httpx.HTTPError as exc:
            log(f"verify 网络异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")

    return wrapper
