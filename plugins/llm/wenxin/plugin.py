"""百度文心 API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

协议：POST {base}/oauth/2.0/token（client_credentials）。
凭证格式为 API_KEY|SECRET_KEY（由子插件自行拆分）。
"""
from __future__ import annotations

from typing import Any

from llm_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "百度文心"
DEFAULT_BASE_URL = "https://aip.baidubce.com"
PROTOCOL = "wenxin"


def _split_key(api_key: str) -> tuple[str, str]:
    """百度文心凭证格式为 API_KEY|SECRET_KEY。"""
    parts = api_key.split("|", 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写 Base URL")
    if api_key:
        client_id, client_secret = _split_key(api_key)
        if not client_id or not client_secret:
            errors.append("百度文心凭证格式应为 API_KEY|SECRET_KEY")
    return errors


def _probe(base_url: str, api_key: str) -> VerifyResult:
    client_id, client_secret = _split_key(api_key)
    url = base_url.rstrip("/") + "/oauth/2.0/token"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    status = resp.status_code
    payload = safe_json(resp)
    if isinstance(payload, dict):
        if payload.get("access_token"):
            info: dict[str, Any] = {}
            if payload.get("expires_in") is not None:
                info["expires_in"] = payload["expires_in"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        if payload.get("error"):
            detail = payload.get("error_description") or payload.get("error")
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    message = f"无法确认凭证状态 (HTTP {status})"
    snippet = body_snippet(resp)
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return _probe(base_url, api_key)


def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
