"""GitHub 令牌验证插件（plugins/dev/ 类别下的独立子插件）。"""
from __future__ import annotations

from typing import Any

from dev_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

NAME = "GitHub"
TOKEN_KEY = "token"
ENDPOINT = "https://api.github.com/user"


def _mask(secret: str) -> str:
    """脱敏展示：保留前 4 位，其余以 * 代替。"""
    secret = secret or ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:4] + "*" * 6


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        errors.append("Token 不能为空")
    return errors


def _verify_github(token: str) -> VerifyResult:
    """GitHub 令牌验证：GET /user（Personal Access Token / OAuth Token 通用）。"""
    with make_client() as client:
        resp = client.get(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "CredentialVault",
            },
        )
    status = resp.status_code
    payload = safe_json(resp)

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
            message=f"请求被拒绝 (HTTP 403)，可能触发限流或令牌权限不足: {body_snippet(resp)}",
        )

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_github(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
