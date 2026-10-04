"""字节扣子 Coze API Key 验证插件（plugins/api/ 类别下的独立子插件）。

自带完整的校验与展示逻辑，只复用 api_common 的通用工具；
复制本目录即可派生出新的服务插件。
"""
from typing import Any

from api_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "字节扣子 Coze"
DEFAULT_BASE_URL = "https://api.coze.cn"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_coze(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/workspaces"
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            total = payload["data"].get("total_count")
            if total is not None:
                info["workspace_count"] = total
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if not DEFAULT_BASE_URL and not base_url:
        errors.append(f"服务「{NAME}」需要填写 Base URL")

    return errors


@guarded
def verify(data):
    """验证字节扣子 Coze API Key 是否有效。"""
    data = data or {}

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"服务「{NAME}」需要填写 Base URL")

    return _verify_coze(base_url, api_key)


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
