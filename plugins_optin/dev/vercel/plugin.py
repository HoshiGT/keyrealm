"""Vercel Access Token 验证插件（plugins_optin/dev/vercel/，opt-in 未用真实凭据实测）。

端点：``GET https://api.vercel.com/v2/user``（Get the User）
鉴权：``Authorization: Bearer <token>``。
判定：
- 200 → valid，account_info 取 ``user.username`` / ``user.email``
- 401 → invalid（官方文档：The request is not authorized）
- 403 → 读响应体：``error.invalidToken`` 为真，或 ``error.code`` / ``error.message``
  为鉴权语义（forbidden / unauthorized / not authorized）→ invalid；
  读不出明确鉴权信号 → error
- 其它 → error（拿不准不猜）
已确认官方文档：vercel.com/docs/rest-api/user/get-the-user（端点、Bearer 鉴权、
响应字段、401/403 语义），2026-10-06 查询；并经代理实测乱写令牌回 403
``{"error":{"code":"forbidden","message":"Not authorized","invalidToken":true}}``。

参考文档：https://vercel.com/docs/rest-api/user/get-the-user
"""
from __future__ import annotations

from typing import Any

from dev_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "Vercel"
TOKEN_KEY = "token"
ENDPOINT = "https://api.vercel.com/v2/user"

# 403 响应体里能确定「就是令牌问题」的 error.code / message 关键字
_AUTH_CODES = ("forbidden", "unauthorized", "not_authenticated", "invalid_token")


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


def _verify_vercel(token: str) -> VerifyResult:
    """Vercel 令牌验证：GET /v2/user。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        user = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict):
            for key in ("username", "email"):
                value = user.get(key)
                if value:
                    info[key] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    if status == 403:
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            if err.get("invalidToken") is True:
                return VerifyResult(status="invalid", message="凭证无效 (HTTP 403: invalid token)")
            code = str(err.get("code") or "").lower()
            message = str(err.get("message") or "").lower()
            if code in _AUTH_CODES or "not authorized" in message or "invalid token" in message:
                return VerifyResult(status="invalid", message="凭证无效 (HTTP 403)")
        return _unconfirmed(status, body_snippet(resp))

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_vercel(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
