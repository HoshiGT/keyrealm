"""QQ 频道机器人凭证验证插件（plugins/im/ 类别下的独立子插件）。

端点：POST https://bots.qq.com/app/getAppAccessToken
以 appId + clientSecret 换取 AppAccessToken。
"""
from im_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE = "qq_bot"
NAME = "QQ 频道机器人"
REQUIRED = (("app_id", "App ID"), ("client_secret", "Client Secret"))
DISPLAY_FIELD = "app_id"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _error_detail(payload) -> str:
    """尽力从错误响应中提取可读原因（飞书/钉钉/QQ 均有 message 类字段）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "errmsg", "description", "error_description"):
        value = payload.get(key)
        if value:
            return str(value)
    code = payload.get("code")
    if code is None:
        code = payload.get("errcode")
    return f"错误码 {code}" if code is not None else ""


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    return errors


def _verify(data: dict) -> VerifyResult:
    app_id = (data.get("app_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    url = "https://bots.qq.com/app/getAppAccessToken"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"appId": app_id, "clientSecret": client_secret},
        )
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
        return VerifyResult(status="valid", message="凭证有效")
    if isinstance(payload, dict):
        detail = _error_detail(payload)
        if detail:
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify(data)


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
