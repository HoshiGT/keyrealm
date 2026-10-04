"""HuggingFace 访问令牌验证插件（plugins/dev/ 类别下的独立子插件）。"""
from __future__ import annotations

from typing import Any

from dev_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

NAME = "HuggingFace"
TOKEN_KEY = "token"
ENDPOINT = "https://huggingface.co/api/whoami-v2"


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


def _verify_huggingface(token: str) -> VerifyResult:
    """HuggingFace 令牌验证：GET /api/whoami-v2。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = safe_json(resp)

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

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_huggingface(token)


# ── 可选操作：查看账号信息 ───────────────────────────────

def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 token 时才提供）。"""
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
    """执行操作：查看 HuggingFace 账号信息（只读，绝不回显 token）。"""
    data = data or {}
    params = params or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return {"ok": False, "error": "缺少 Token"}
    try:
        if action_id != "account_info":
            return {"ok": False, "error": "未知操作"}
        with make_client() as client:
            resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            return {"ok": False, "error": "凭证无效 (HTTP 401)"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"请求失败 (HTTP {resp.status_code})"}
        payload = safe_json(resp) or {}
        lines = [
            f"名称：{payload.get('name') or '-'}",
            f"类型：{payload.get('type') or '-'}",
        ]
        plan = payload.get("plan")
        if plan:
            lines.append(f"计划：{plan}")
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
