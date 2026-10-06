"""FOFA 密钥验证插件。

- 端点: GET https://fofa.info/api/v1/info/my?email=<email>&key=<api_key>
- 鉴权: 查询参数 email + key
- 判定依据: 200 且 body.error == False 视为有效(取 fcoin / vip_level);
  200 且 body.error == True 视为无效(取 errmsg, 并净化其中可能回显的 email/key); 其余状态视为不确定。
参考文档：https://fofa.info/api
"""

from api_common import VerifyResult, guarded, make_client, safe_json, body_snippet

NAME = "FOFA"
INFO_URL = "https://fofa.info/api/v1/info/my"


def _mask_secret(value):
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _sanitize(text, email, key):
    text = (text or "").strip()
    for secret in (email, key):
        if secret:
            text = text.replace(secret, "***")
    if len(text) > 160:
        text = text[:160] + "…"
    return text


def _unconfirmed(status, snippet):
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data):
    if not data:
        return ["配置数据为空"]
    errors = []
    if not (data.get("email") or "").strip():
        errors.append("邮箱 不能为空")
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    return errors


def _verify(data):
    email = (data.get("email") or "").strip()
    key = (data.get("api_key") or "").strip()
    with make_client() as client:
        resp = client.get(INFO_URL, params={"email": email, "key": key})
    status = resp.status_code
    if status == 200:
        payload = safe_json(resp) or {}
        if payload.get("error") is True:
            errmsg = _sanitize(str(payload.get("errmsg") or "账号无效"), email, key)
            return VerifyResult(status="invalid", message=f"凭证无效: {errmsg}")
        if payload.get("error") is False:
            info = {}
            if payload.get("fcoin") is not None:
                info["fcoin"] = payload.get("fcoin")
            if payload.get("vip_level") is not None:
                info["vip_level"] = payload.get("vip_level")
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        return _unconfirmed(status, body_snippet(resp))
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data):
    data = data or {}
    if not (data.get("email") or "").strip() or not (data.get("api_key") or "").strip():
        return VerifyResult(status="invalid", message="邮箱 或 API Key 为空")
    return _verify(data)


def get_display_summary(data):
    data = data or {}
    email = (data.get("email") or "").strip()
    key = (data.get("api_key") or "").strip()
    parts = [NAME]
    if email:
        parts.append(email)
    if key:
        parts.append(_mask_secret(key))
    return " · ".join(parts)
