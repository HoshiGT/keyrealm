"""Notion 密钥验证插件。

- 端点: GET https://api.notion.com/v1/users/me
- 鉴权: Authorization: Bearer <api_token>; 必须携带 Notion-Version 头
- 判定依据: 200 视为有效(取 name / type); 401 视为无效; 400/403 视为不确定。
- Notion-Version 固定为 2022-06-28(实测仍被接受)。

参考文档：https://developers.notion.com/reference/get-self
"""

from api_common import VerifyResult, guarded, make_client, safe_json, body_snippet

NAME = "Notion"
USERS_ME_URL = "https://api.notion.com/v1/users/me"
NOTION_VERSION = "2022-06-28"


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
    return [] if (data.get("api_token") or "").strip() else ["API Token 不能为空"]


def _verify(data):
    token = (data.get("api_token") or "").strip()
    with make_client() as client:
        resp = client.get(
            USERS_ME_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
            },
        )
    status = resp.status_code
    if status == 200:
        payload = safe_json(resp) or {}
        info = {}
        if payload.get("name"):
            info["name"] = payload.get("name")
        if payload.get("type"):
            info["type"] = payload.get("type")
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data):
    data = data or {}
    if not (data.get("api_token") or "").strip():
        return VerifyResult(status="invalid", message="API Token 为空")
    return _verify(data)


def get_display_summary(data):
    data = data or {}
    token = (data.get("api_token") or "").strip()
    return f"{NAME} · {_mask_secret(token)}" if token else NAME
