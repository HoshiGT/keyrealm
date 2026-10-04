"""即时通讯类别共享工具：各服务子插件共用的请求封装与结果类型。

plugins/im/ 下的每个子插件（feishu、dingtalk、telegram……）都是独立插件，
**自带完整的端点与判定逻辑**，只通过 `from im_common import ...` 复用本模块的
通用工具；删除任一子插件目录不影响其他子插件运行，新增服务也无需改动本模块。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。

安全约定：
- account_info 与 display_summary 只输出非敏感信息（应用 ID、机器人 ID、
  渠道名等公开标识），绝不回显 app_secret / bot_token / webhook_url /
  client_secret / credential 等密钥明文；
- Telegram Bot、Discord Webhook 与钉钉旧版端点的密钥位于 URL / 查询串中，
  因此 guarded() 兜底异常时只返回异常类型名，绝不透出异常文本——异常文本
  通常包含完整 URL，会把密钥写进凭据记录。
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
    print(f"[im] {message}", file=sys.stderr)


def proxy() -> Optional[str]:
    """验证代理：优先 VAULT_PROXY（应用设置写入），其次标准代理环境变量。"""
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def make_client(follow_redirects: bool = True) -> httpx.Client:
    """构造统一超时与代理的 httpx 客户端。"""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=follow_redirects, proxy=proxy())


def safe_json(response: httpx.Response) -> Optional[dict]:
    """解析 JSON 响应；非 JSON 或非对象一律返回 None。"""
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(response: httpx.Response) -> str:
    """响应正文压缩成单行并截断，用于错误消息。"""
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def guarded(fn):
    """子插件 verify 的异常兜底装饰器，保证 verify 永不抛出。

    只保留异常类型名：密钥位于 URL / 查询串的服务（Telegram Bot、Discord
    Webhook、钉钉旧版端点）异常文本里带完整 URL，直接透出会把密钥写进凭据。
    """
    def wrapper(data=None):
        try:
            return fn(data or {})
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
        except httpx.HTTPError as exc:
            # 异常文本可能包含带密钥的 URL，只保留异常类型
            return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")

    wrapper.__name__ = getattr(fn, "__name__", "verify")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper
