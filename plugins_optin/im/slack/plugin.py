"""Slack 令牌验证插件（plugins_optin/im/slack/，opt-in 未用真实凭据实测）。

端点：``POST https://slack.com/api/auth.test``
鉴权：``Authorization: Bearer <token>``（官方文档同时支持 Authorization header 与
POST 参数两种写法，这里取 header；``xoxb-`` 是 Bot Token，``xoxp-`` 是 User Token）。
判定：
- ``ok == true`` → valid，account_info 取 ``team`` / ``user`` / ``team_id``
- ``ok == false`` 且 ``error`` 属鉴权类（``invalid_auth`` / ``token_revoked`` /
  ``account_inactive`` / ``not_authed`` / ``token_expired``）→ invalid
- ``ok == false`` 但 ``error`` 是其它（``ratelimited`` / ``internal_error`` /
  ``fatal_error`` 等）→ error（拿不准不猜）
- HTTP 非 200（429 / 5xx 等）→ error

**注意**：Slack Web API 即使鉴权失败也返回 **HTTP 200**，判定必须看响应体里的
``ok`` 字段，不能看 HTTP 状态码 —— 这是 Slack 的通用约定（官方文档反复提醒
“always check the value of the ok parameter”）。

已确认官方文档：api.slack.com/methods/auth.test（现重定向至
docs.slack.dev/reference/methods/auth.test），2026-10-06 查询 —— 确认了请求方式、
成功响应字段（team / user / team_id / user_id / bot_id）与鉴权类错误取值
（not_authed / invalid_auth / account_inactive / token_revoked / token_expired）。
「HTTP 200 但 ok=false」属于 Slack Web API 通用行为，官方文档未单列状态码，
仅强调按 ``ok`` 判定。

参考文档：https://docs.slack.dev/reference/methods/auth.test
"""
from __future__ import annotations

from typing import Any

from im_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "Slack"
TOKEN_KEY = "api_token"
ENDPOINT = "https://slack.com/api/auth.test"

# 鉴权类错误：auth.test 官方文档列出的取值，出现即令牌失效
_AUTH_ERRORS = {"not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"}


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
    if not str(data.get(TOKEN_KEY) or "").strip():
        errors.append("API Token 不能为空")
    return errors


def _verify_slack(token: str) -> VerifyResult:
    """Slack 令牌验证：POST /api/auth.test，按响应体 ``ok`` 判定。"""
    with make_client() as client:
        resp = client.post(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = safe_json(resp)

    # Slack Web API 约定：即使鉴权失败也回 HTTP 200，必须看 body 里的 ok
    if status == 200 and isinstance(payload, dict):
        if payload.get("ok") is True:
            info: dict[str, Any] = {}
            for key in ("team", "user", "team_id"):
                value = payload.get(key)
                if value:
                    info[key] = value
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        error = payload.get("error")
        if error in _AUTH_ERRORS:
            return VerifyResult(status="invalid", message=f"凭证无效 ({error})")
        # ok=false 但 error 不是鉴权类（ratelimited / internal_error 等）→ 拿不准
        return VerifyResult(status="error", message=f"无法确认凭证状态 ({error or 'ok=false'})")

    if status == 429:
        return VerifyResult(status="error", message=f"Slack 限流（HTTP {status}），请稍后重试")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = str(data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="API Token 为空")
    return _verify_slack(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本，令牌只打码不回显。"""
    data = data or {}
    secret = str(data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
