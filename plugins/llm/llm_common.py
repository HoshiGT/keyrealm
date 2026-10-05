"""LLM 类别共享模块：**最底层通用工具** + 子插件原型接口。

只提供与厂商、协议都无关的最底层能力：

- 结果类型 `VerifyResult`
- 代理读取 `proxy`、HTTP 客户端 `make_client`
- 响应解析 `safe_json` / `body_snippet`
- 异常兜底 `guarded`、日志 `log`
- 常量 `TIMEOUT` / `_BODY_LIMIT`

协议级工具（探针、简单对话等跨厂商完全相同的能力）放在**独立的工具模块**
`plugins/llm/llm_protocols.py`，由需要的子插件按需 `from llm_protocols import ...`。

各厂商的默认地址、模型名、余额端点与解析等**专属逻辑**一律写在
`plugins/llm/<厂商>/plugin.py` 内（厂商之间允许重复，刻意为之）——
新增厂商只需照抄同级子插件，无需修改本模块或协议工具模块。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import logging
import os
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


_logger = logging.getLogger("plugin.llm")


def log(message: str) -> None:
    """插件日志 → 应用统一日志（根 logger 落盘到 data/logs/credential_vault.log）。

    不要改回 print(..., file=sys.stderr)：打包成 GUI 程序后没有 stdio，
    输出会被静默丢弃（在 windowed 模式下写 stderr 甚至直接抛异常），
    结果就是插件的告警与异常在系统日志里完全不可见。
    """
    _logger.info("%s", message)


# ── 请求封装 ─────────────────────────────────────────────

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
    """装饰器：兜底 verify/validate 未预期异常 → error 结果，保证不抛出。

    异常消息只暴露异常类型名，避免把 URL（部分厂商密钥在 query 中）
    或响应正文带进错误提示。
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
        except httpx.HTTPError as exc:
            log(f"verify 网络异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")
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


def verify(data: dict) -> VerifyResult:
    """子插件实现：验证凭据，返回 VerifyResult（valid / invalid / error）。"""
    raise NotImplementedError


def get_display_summary(data: dict) -> str:
    """子插件实现：凭据列表中的简短展示文本。"""
    return ""


def actions(data: dict) -> list:
    """子插件可选实现：声明可用操作（如简单对话）；返回 [] 表示无操作。"""
    return []


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """子插件可选实现：执行操作，返回 {"ok": bool, "output"/"error": str}。"""
    return {"ok": False, "error": "未实现"}
