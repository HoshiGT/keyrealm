"""API 服务类别共享模块：仅供各服务子插件复用的通用工具。

plugins/api/ 下的每个子插件（firecrawl、tavily、coze……）都是独立插件，
自带完整的验证实现，通过 `from api_common import ...` 复用本模块的通用工具；
删除任一子插件目录不影响其他子插件运行，新增服务插件时也无需改动本文件。

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
    """打印插件日志到 stderr。"""
    print(f"[api] {message}", file=sys.stderr)


def safe_json(response: httpx.Response) -> Optional[dict]:
    """尽力解析 JSON 响应；非 dict 或解析失败时返回 None。"""
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(response: httpx.Response) -> str:
    """压缩后的响应正文片段，用于拼装错误消息。"""
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def proxy() -> Optional[str]:
    """验证代理：优先 VAULT_PROXY（应用设置写入），其次标准代理环境变量。"""
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def make_client(follow_redirects: bool = True) -> httpx.Client:
    """构造统一配置的 httpx 客户端（超时 + 代理）。"""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=follow_redirects, proxy=proxy())


def guarded(fn):
    """装饰 verify：插件边界兜底，保证不抛异常，异常只暴露类型名。"""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
        except httpx.HTTPError as exc:
            log(f"verify 网络请求失败: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")

    return wrapper
