"""Gitee 访问令牌验证插件（plugins_optin/dev/gitee/，opt-in 未用真实凭据实测）。

端点：``GET https://gitee.com/api/v5/user``（当前认证用户）
鉴权：``access_token`` 作为 query 参数（Gitee API v5 私人令牌的通用方式）。
判定：
- 200 → valid，account_info 取 ``login`` / ``name``
- 401 → invalid（实测乱写令牌回 401 ``{"message":"... Access token does not exist"}``）
- 其它 → error（拿不准不猜）
已确认：端点与 401 行为经代理实测确认（2026-10-06）；成功响应字段 login/name 照
Gitee API v5 User 对象，未用真实令牌验证。
注意：令牌在 query 中，任何进入 message 的响应文本都先净化，绝不回显 token。

参考文档：https://gitee.com/api/v5/swagger
"""
from __future__ import annotations

from typing import Any

from dev_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "Gitee"
TOKEN_KEY = "token"
ENDPOINT = "https://gitee.com/api/v5/user"


def _mask(secret: str) -> str:
    """脱敏展示：保留前 4 位，其余以 * 代替。"""
    secret = secret or ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:4] + "*" * 6


def _purge(text: str, token: str) -> str:
    """从文本里抹掉 token 明文（响应若意外回显 query 里的令牌时兜底）。"""
    if token and text:
        text = text.replace(token, "***")
    return text


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


def _verify_gitee(token: str) -> VerifyResult:
    """Gitee 令牌验证：GET /api/v5/user?access_token=<token>。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, params={"access_token": token})
    status = resp.status_code
    payload = safe_json(resp)
    snippet = _purge(body_snippet(resp), token)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("login", "name"):
                value = payload.get(key)
                if value:
                    info[key] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    return _unconfirmed(status, snippet)


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_gitee(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
