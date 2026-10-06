"""Weights & Biases API Key 验证插件（plugins/dev/ 类别下的独立子插件）。

参考文档：https://docs.coreweave.com/models/ref/python/public-api
"""
from __future__ import annotations

from typing import Any

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json

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


# ── 可选操作：查看账号信息 ───────────────────────────────

_VIEWER_QUERY = "query { viewer { username email } }"


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 api_key 时才提供）。"""
    data = data or {}
    if not (data.get(TOKEN_KEY) or "").strip():
        return []
    return [{
        "id": "account_info",
        "name": "查看账号信息",
        "kind": "form",
        "submit": "查询",
        "fields": [],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看 W&B 账号信息（只读，绝不回显 api_key）。"""
    data = data or {}
    params = params or {}
    api_key = (data.get(TOKEN_KEY) or "").strip()
    if not api_key:
        return {"ok": False, "error": "缺少 API Key"}
    try:
        if action_id != "account_info":
            return {"ok": False, "error": "未知操作"}
        with make_client() as client:
            resp = client.post(
                ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": _VIEWER_QUERY},
            )
        if resp.status_code in (401, 403):
            return {"ok": False, "error": f"凭证无效 (HTTP {resp.status_code})"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"请求失败 (HTTP {resp.status_code})"}
        payload = safe_json(resp) or {}
        viewer = None
        node = payload.get("data")
        if isinstance(node, dict):
            viewer = node.get("viewer")
        if not isinstance(viewer, dict) or not viewer:
            return {"ok": False, "error": "凭证无效或无 viewer 数据"}
        lines = [
            f"用户名：{viewer.get('username') or '-'}",
            f"邮箱：{viewer.get('email') or '-'}",
        ]
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
