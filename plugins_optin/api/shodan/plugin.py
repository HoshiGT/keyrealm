"""Shodan 密钥验证插件。

- 端点: GET https://api.shodan.io/api-info?key=<api_key>
- 鉴权: 查询参数 key
- 判定依据: 200 视为有效(取 plan / query_credits); 401 视为无效(实测无效 key 返回 HTML 401); 403/429 视为不确定。
参考文档：https://developer.shodan.io/api
"""

from api_common import VerifyResult, guarded, make_client, safe_json, body_snippet

NAME = "Shodan"
INFO_URL = "https://api.shodan.io/api-info"


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
        resp = client.get(INFO_URL, params={"key": key})
    status = resp.status_code
    if status == 200:
        payload = safe_json(resp) or {}
        info = {}
        if payload.get("plan"):
            info["plan"] = payload.get("plan")
        if payload.get("query_credits") is not None:
            info["query_credits"] = payload.get("query_credits")
        if payload.get("scan_credits") is not None:
            info["scan_credits"] = payload.get("scan_credits")
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
    if status in (403, 429):
        return _unconfirmed(status, body_snippet(resp))
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
