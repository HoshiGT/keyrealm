"""Airtable 密钥验证插件。

- 端点: GET https://api.airtable.com/v0/meta/whoami
- 鉴权: Authorization: Bearer <api_key>
- 判定依据: 200 视为有效(取 id); 401 视为无效; 其余状态视为不确定。
参考文档：https://airtable.com/developers/web/api/get-user-id-scopes
参考文档：https://airtable.com/developers/web/api/authentication
"""

from api_common import VerifyResult, guarded, make_client, safe_json, body_snippet

NAME = "Airtable"
WHOAMI_URL = "https://api.airtable.com/v0/meta/whoami"


def _mask_secret(value):
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _unconfirmed(status, snippet):
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data):
    if not data:
        return ["配置数据为空"]
    return [] if (data.get("api_key") or "").strip() else ["API Key 不能为空"]


def _verify(data):
    key = (data.get("api_key") or "").strip()
    with make_client() as client:
        resp = client.get(WHOAMI_URL, headers={"Authorization": f"Bearer {key}"})
    status = resp.status_code
    if status == 200:
        payload = safe_json(resp) or {}
        info = {}
        if payload.get("id"):
            info["account_id"] = payload.get("id")
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data):
    data = data or {}
    if not (data.get("api_key") or "").strip():
        return VerifyResult(status="invalid", message="API Key 为空")
    return _verify(data)


def get_display_summary(data):
    data = data or {}
    key = (data.get("api_key") or "").strip()
    return f"{NAME} · {_mask_secret(key)}" if key else NAME
