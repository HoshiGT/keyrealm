"""Cloudflare API Token / Global API Key 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：支持 token 与 global 两种认证模式，调用只读的
/user 或 /user/tokens/verify 接口，不产生任何资源变更。
仅从 cloud_common 复用通用工具。
"""
from __future__ import annotations

from typing import Any, Optional

from cloud_common import VerifyResult, body_snippet, guarded, make_client, safe_json

# Cloudflare API 基地址
API_BASE = "https://api.cloudflare.com/client/v4"


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _use_global(data: dict) -> Optional[bool]:
    """判定凭据模式：True=Global API Key，False=API Token，None=未提供可用凭据。

    显式 auth_mode 优先；未选择时按已填字段推断。
    """
    auth_mode = _field(data, "auth_mode").lower()
    api_token = _field(data, "api_token")
    email = _field(data, "email")
    global_key = _field(data, "global_api_key")

    if auth_mode == "global":
        return True
    if auth_mode == "token":
        return False
    if api_token:
        return False
    if email and global_key:
        return True
    return None


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []

    use_global = _use_global(data)
    if use_global is None:
        errors.append("请选择认证方式，并填写对应的凭据（API Token，或 Email + Global API Key）")
    elif use_global:
        if not _field(data, "email"):
            errors.append("Global API Key 模式下 Email 不能为空")
        if not _field(data, "global_api_key"):
            errors.append("Global API Key 模式下 Global API Key 不能为空")
    elif not _field(data, "api_token"):
        errors.append("API Token 模式下 API Token 不能为空")

    return errors


@guarded
def verify(data):
    use_global = _use_global(data)
    if use_global is None:
        return VerifyResult(
            status="invalid",
            message="未提供可用的 Cloudflare 凭据（需要 API Token，或 Email + Global API Key）",
        )

    if use_global:
        email = _field(data, "email")
        global_key = _field(data, "global_api_key")
        if not email or not global_key:
            return VerifyResult(
                status="invalid", message="Global API Key 模式需要同时填写 Email 与 Global API Key"
            )
        url = f"{API_BASE}/user"
        headers = {"X-Auth-Email": email, "X-Auth-Key": global_key}
        mode = "global"
    else:
        api_token = _field(data, "api_token")
        if not api_token:
            return VerifyResult(status="invalid", message="API Token 模式下 API Token 不能为空")
        url = f"{API_BASE}/user/tokens/verify"
        headers = {"Authorization": f"Bearer {api_token}"}
        mode = "token"

    with make_client() as client:
        resp = client.get(url, headers=headers)

    status = resp.status_code
    payload = safe_json(resp) or {}
    success = payload.get("success")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

    if success is True:
        info: dict[str, Any] = {}
        if mode == "token":
            if result.get("id"):
                info["token_id"] = result["id"]
            if result.get("status"):
                info["token_status"] = result["status"]
        else:
            if result.get("email"):
                info["email"] = result["email"]
            if result.get("id"):
                info["user_id"] = result["id"]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 认证失败：success=false（通常 HTTP 401/403）
    if success is False or status in (401, 403):
        errors = payload.get("errors")
        detail = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = errors[0].get("message") or ""
        message = "凭证无效" if not detail else f"凭证无效: {detail}"
        return VerifyResult(status="invalid", message=message)

    return _unconfirmed(status, body_snippet(resp))


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    data = data or {}
    # 展示用推断：显式 global 或「只填了 Global API Key」都按 global 展示
    auth_mode = _field(data, "auth_mode").lower()
    is_global = auth_mode == "global" or (
        auth_mode != "token"
        and bool(_field(data, "global_api_key"))
        and not _field(data, "api_token")
    )
    mode = "Global API Key" if is_global else "API Token"
    return f"Cloudflare | {mode}"
