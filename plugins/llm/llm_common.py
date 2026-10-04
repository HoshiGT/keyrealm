"""LLM 类别共享模块：通用工具与协议级探针。

plugins/llm/ 下的每个子插件（openai、deepseek……）都是独立插件，
通过 `from llm_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

本模块**只提供与厂商无关的通用能力**：

- 结果类型 `VerifyResult`、异常兜底 `guarded`、日志 `log`
- 代理读取 `proxy`、HTTP 客户端 `make_client`
- 响应解析 `safe_json` / `body_snippet`
- 协议级探针（多厂商共用，非某厂商专属）：
  `probe_openai_models` / `probe_anthropic_models` /
  `probe_openai_chat` / `probe_anthropic_chat`

各厂商的默认地址、模型名、协议选择、余额端点与解析逻辑一律写在
plugins/llm/<厂商>/plugin.py 内（厂商之间允许重复，刻意为之）——
**新增厂商只需照抄同级子插件，无需修改本模块**。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

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

# 欠费/余额不足判定关键字（出现在错误正文中）
_INSUFFICIENT_BALANCE_KEYWORDS = (
    "余额不足",
    "充值",
    "insufficient",
    "no balance",
    "out of balance",
    "1113",  # 智谱错误码：余额不足
)


def log(message: str) -> None:
    print(f"[llm] {message}", file=sys.stderr)


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
            return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常（{type(exc).__name__}）")

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


# ── 可选操作：简单对话（供厂商插件声明 actions/run_action 复用）────

def chat_completion(base_url: str, api_key: str, model: str, messages: list,
                    protocol: str = "openai", max_tokens: int = 512) -> dict:
    """发起一次简单对话。返回 {"ok": bool, "output": str} 或 {"ok": False, "error": str}。"""
    if not model:
        return {"ok": False, "error": "未指定模型"}
    if not api_key:
        return {"ok": False, "error": "缺少 API Key"}
    root = base_url.rstrip("/")
    try:
        with make_client() as client:
            if protocol == "anthropic":
                resp = client.post(
                    root + "/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "max_tokens": max_tokens, "messages": messages},
                )
            else:
                resp = client.post(
                    root + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": max_tokens},
                )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(TIMEOUT)} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）"}

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict):
        if protocol == "anthropic":
            blocks = payload.get("content")
            if isinstance(blocks, list):
                text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
                if text.strip():
                    return {"ok": True, "output": text.strip()}
        else:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict) and message.get("content"):
                    return {"ok": True, "output": str(message["content"]).strip()}
        return {"ok": False, "error": "响应中没有可读内容"}
    if status in (401, 403):
        return {"ok": False, "error": f"鉴权失败 (HTTP {status})"}
    snippet = body_snippet(resp)
    return {"ok": False, "error": f"HTTP {status}" + (f": {snippet}" if snippet else "")}


# ── 协议级探针（通用，不含厂商专属逻辑） ─────────────────

def probe_openai_models(base_url: str, api_key: str) -> VerifyResult:
    """OpenAI 协议：GET {base}/models，Bearer 认证。"""
    url = base_url.rstrip("/") + "/models"
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    return _interpret_simple(resp)


def probe_anthropic_models(base_url: str, api_key: str, verify_path: str = "/models") -> VerifyResult:
    """Anthropic 协议：GET {base}{verify_path}，x-api-key 认证。"""
    url = base_url.rstrip("/") + verify_path
    with make_client() as client:
        resp = client.get(
            url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    return _interpret_simple(resp)


def probe_openai_chat(base_url: str, api_key: str, model: str) -> VerifyResult:
    """OpenAI 协议：POST {base}/chat/completions（消耗极小额度，可识别欠费）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
    return _interpret_chat(resp)


def probe_anthropic_chat(base_url: str, api_key: str, model: str) -> VerifyResult:
    """Anthropic 协议：POST {base}/v1/messages（可识别欠费）。"""
    url = base_url.rstrip("/") + "/v1/messages"
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    with make_client() as client:
        resp = client.post(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
        )
    return _interpret_chat(resp)


# ── 响应判定 ─────────────────────────────────────────────

def _interpret_simple(resp: httpx.Response) -> VerifyResult:
    """简单 GET 探针（models 类）的通用判定。"""
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, body_snippet(resp))


def _interpret_chat(resp: httpx.Response) -> VerifyResult:
    """chat 探针判定：额外识别 429 余额不足（欠费）与速率限制。"""
    status = resp.status_code
    payload = safe_json(resp)
    snippet = body_snippet(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        if _looks_insufficient_balance(snippet):
            # 欠费不算失效：密钥本身有效，仅账户余额不足
            return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用资源包")
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    if status == 402:
        # 402 Payment Required：欠费不算失效
        return VerifyResult(status="valid", message="凭证有效，但账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, snippet)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _looks_insufficient_balance(snippet: str) -> bool:
    text = (snippet or "").lower()
    return any(kw in text for kw in _INSUFFICIENT_BALANCE_KEYWORDS)


def _extract_account_info(payload: Any) -> Optional[dict]:
    """从响应中提取账号信息。

    LLM 的「附加信息」只展示余额 / 使用率，故不再回传模型数量等噪声字段；
    余额由各厂商 plugin.py 自行查询后合并进 account_info。
    """
    if not isinstance(payload, dict):
        return None

    info: dict[str, Any] = {}
    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]

    return info or None
