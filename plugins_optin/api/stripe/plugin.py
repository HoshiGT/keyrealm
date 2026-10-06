"""Stripe 密钥验证插件。

- 端点: GET https://api.stripe.com/v1/balance
- 鉴权: Authorization: Bearer <secret_key>
- 判定依据: 200 且 body.object == "balance" 视为有效; 401 视为无效; 403 视为受限(不确定); 其余状态视为不确定。
参考文档：https://docs.stripe.com/api/balance
"""

from api_common import VerifyResult, guarded, make_client, safe_json, body_snippet

NAME = "Stripe"
BALANCE_URL = "https://api.stripe.com/v1/balance"


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
    return [] if (data.get("secret_key") or "").strip() else ["Secret Key 不能为空"]


def _verify(data):
    key = (data.get("secret_key") or "").strip()
    with make_client() as client:
        resp = client.get(BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
    status = resp.status_code
    if status == 200:
        payload = safe_json(resp) or {}
        if payload.get("object") != "balance":
            return _unconfirmed(status, body_snippet(resp))
        info = {}
        available = payload.get("available") or []
        pending = payload.get("pending") or []
        if isinstance(available, list):
            info["available_currencies"] = len(available)
        if isinstance(pending, list):
            info["pending_currencies"] = len(pending)
        if "livemode" in payload:
            info["livemode"] = bool(payload.get("livemode"))
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")
    if status == 403:
        return VerifyResult(status="error", message="凭证受限，无法访问 balance (HTTP 403)")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data):
    data = data or {}
    if not (data.get("secret_key") or "").strip():
        return VerifyResult(status="invalid", message="Secret Key 为空")
    return _verify(data)


def get_display_summary(data):
    data = data or {}
    key = (data.get("secret_key") or "").strip()
    return f"{NAME} · {_mask_secret(key)}" if key else NAME
