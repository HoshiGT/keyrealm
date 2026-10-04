"""开发者平台类别共享模块：各服务验证处理器。

plugins/dev/ 下的每个子插件（github、huggingface、wandb）都是独立插件，
通过 `from dev_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持 GitHub（代码托管）、HuggingFace（模型托管）、
Weights & Biases（实验跟踪）。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

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


SERVICE_CONFIGS = {
    "github": {
        "name": "GitHub",
        "token_key": "token",
        "endpoint": "https://api.github.com/user",
    },
    "huggingface": {
        "name": "HuggingFace",
        "token_key": "token",
        "endpoint": "https://huggingface.co/api/whoami-v2",
    },
    "wandb": {
        "name": "Weights & Biases",
        "token_key": "api_key",
        "endpoint": "https://api.wandb.ai/graphql",
    },
}


def _log(message: str) -> None:
    print(f"[dev] {message}", file=sys.stderr)


def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _mask(secret: str) -> str:
    """脱敏展示：保留前 4 位，其余以 * 代替。"""
    secret = secret or ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:4] + "*" * 6


# ── 各服务验证 ───────────────────────────────────────────

def _verify_github(data: dict) -> VerifyResult:
    """GitHub 令牌验证：GET /user（Personal Access Token / OAuth Token 通用）。"""
    token = (data.get("token") or "").strip()
    url = SERVICE_CONFIGS["github"]["endpoint"]
    with _client() as client:
        resp = client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "CredentialVault",
            },
        )
    status = resp.status_code
    payload = _safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("login", "name", "type"):
                value = payload.get(key)
                if value:
                    info[key] = value
        # OAuth / fine-grained 令牌会回传授权范围，便于确认权限面
        scopes = resp.headers.get("X-OAuth-Scopes")
        if scopes:
            info["scopes"] = scopes
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    if status == 403:
        # 403 多为触发 API 速率限制；剩余配额为 0 时给出更明确的提示
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return VerifyResult(
                status="error",
                message="请求被 GitHub 限流（API 速率限制已用尽），暂时无法确认凭证状态",
            )
        return VerifyResult(
            status="error",
            message=f"请求被拒绝 (HTTP 403)，可能触发限流或令牌权限不足: {_body_snippet(resp)}",
        )

    return _unconfirmed(status, _body_snippet(resp))


def _verify_huggingface(data: dict) -> VerifyResult:
    """HuggingFace 令牌验证：GET /api/whoami-v2。"""
    token = (data.get("token") or "").strip()
    url = SERVICE_CONFIGS["huggingface"]["endpoint"]
    with _client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = _safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("name", "type"):
                value = payload.get(key)
                if value:
                    info[key] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    return _unconfirmed(status, _body_snippet(resp))


def _verify_wandb(data: dict) -> VerifyResult:
    """Weights & Biases API Key 验证：POST /graphql 查询 viewer。"""
    api_key = (data.get("api_key") or "").strip()
    url = SERVICE_CONFIGS["wandb"]["endpoint"]
    with _client() as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": "query { viewer { username email } }"},
        )
    status = resp.status_code
    payload = _safe_json(resp)

    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")

    if status == 200:
        viewer = None
        if isinstance(payload, dict):
            data_node = payload.get("data")
            if isinstance(data_node, dict):
                viewer = data_node.get("viewer")
        if isinstance(viewer, dict) and viewer:
            info: dict[str, Any] = {}
            for key in ("username", "email"):
                value = viewer.get(key)
                if value:
                    info[key] = value
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        # HTTP 200 但无 viewer：GraphQL 以 errors 表达认证失败
        return VerifyResult(
            status="invalid",
            message=f"凭证无效或无 viewer 数据: {_body_snippet(resp)}",
        )

    return _unconfirmed(status, _body_snippet(resp))


_SERVICE_VERIFIERS = {
    "github": _verify_github,
    "huggingface": _verify_huggingface,
    "wandb": _verify_wandb,
}


# ── 子插件入口（service 由子插件显式传入） ───────────────

def validate_common(service: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = SERVICE_CONFIGS.get(service)
    if config is None:
        return [f"不支持的服务: {service}"]

    errors: list[str] = []

    secret = (data.get(config["token_key"]) or "").strip()
    if not secret:
        label = "API Key" if config["token_key"] == "api_key" else "Token"
        errors.append(f"{label} 不能为空")

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务凭证是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        token_key = config["token_key"]
        secret = (data.get(token_key) or "").strip()
        if not secret:
            label = "API Key" if token_key == "api_key" else "Token"
            return VerifyResult(status="invalid", message=f"{label} 为空")

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")
        return verifier(data)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        _log(f"verify 网络异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)
    token_key = config.get("token_key", "token")
    secret = (data.get(token_key) or "").strip()
    if not secret:
        return name
    return f"{name} | {_mask(secret)}"
