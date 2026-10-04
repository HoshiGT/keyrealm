"""Weights & Biases API Key 验证插件（plugins/dev/ 类别下的独立子插件）。"""
from __future__ import annotations

from typing import Any

from dev_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

NAME = "Weights & Biases"
TOKEN_KEY = "api_key"
ENDPOINT = "https://api.wandb.ai/graphql"


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
    api_key = (data.get(TOKEN_KEY) or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")
    return errors


def _verify_wandb(api_key: str) -> VerifyResult:
    """Weights & Biases API Key 验证：POST /graphql 查询 viewer。"""
    with make_client() as client:
        resp = client.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": "query { viewer { username email } }"},
        )
    status = resp.status_code
    payload = safe_json(resp)

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
            message=f"凭证无效或无 viewer 数据: {body_snippet(resp)}",
        )

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    api_key = (data.get(TOKEN_KEY) or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    return _verify_wandb(api_key)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
