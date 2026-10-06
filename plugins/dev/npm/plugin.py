"""npm 访问令牌验证插件（plugins/dev/ 类别下的独立子插件）。

端点：``GET https://registry.npmjs.org/-/whoami``
鉴权：``Authorization: Bearer <token>``。
判定：
- 200 → valid，account_info 取 ``username``（响应形如 ``{"username": "..."}``）
- 401 → invalid（实测乱写令牌回 401，空 body）
- 其它 → error（拿不准不猜）
已确认：``/-/whoami`` 是 npm 官方 registry 的 whoami 端点（npm-whoami 命令同样
调用它），成功响应形如 ``{"username": "..."}``；经代理实测乱写令牌回 401（空 body），
故 401 判无效、200 判有效。

参考文档：https://docs.npmjs.com/cli/v12/commands/npm-whoami
"""
from __future__ import annotations

from typing import Any

from dev_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "npm"
TOKEN_KEY = "token"
ENDPOINT = "https://registry.npmjs.org/-/whoami"


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


def _verify_npm(token: str) -> VerifyResult:
    """npm 令牌验证：GET /-/whoami。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            username = payload.get("username")
            if username:
                info["username"] = username
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401)")

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = (data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Token 为空")
    return _verify_npm(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    secret = (data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
