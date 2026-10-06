"""LINE 频道访问令牌验证插件（plugins_optin/im/line/，opt-in 未用真实凭据实测）。

端点：``GET https://api.line.me/v2/bot/info``
鉴权：``Authorization: Bearer <channel_access_token>``（LINE Messaging API 通用鉴权）。
判定：
- 200 → valid，account_info 取 ``userId`` / ``displayName`` / ``basicId``
- 401 → invalid（官方错误表：401 Unauthorized = “Invalid channel access token”）
- 403 → error（官方错误表：403 Forbidden = “缺乏访问该资源的权限”，属权限问题
  而非令牌失效）
- 其它 → error（拿不准不猜）

已确认官方文档：developers.line.biz/en/reference/messaging-api/（Messaging API
reference，含「Get bot info」接口及 Common specifications「Error responses /
Status codes」章节）。401 = Invalid channel access token、403 = 缺乏权限取自该官方
参考的通用规范章节；本环境 fetch 该页只能拿到目录（JS 渲染），接口详情与 401/403
错误表的具体字段未逐字回显，故「有效」路径与字段名未用真实令牌实测。

参考文档：https://developers.line.biz/en/reference/messaging-api/
"""
from __future__ import annotations

from typing import Any

from im_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "LINE"
TOKEN_KEY = "channel_access_token"
ENDPOINT = "https://api.line.me/v2/bot/info"


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
        errors.append("Channel Access Token 不能为空")
    return errors


def _verify_line(token: str) -> VerifyResult:
    """LINE 频道令牌验证：GET /v2/bot/info。"""
    with make_client() as client:
        resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    status = resp.status_code
    payload = safe_json(resp)

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("userId", "displayName", "basicId"):
                value = payload.get(key)
                if value:
                    info[key] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status == 401:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 401，Invalid channel access token)")

    if status == 403:
        return VerifyResult(status="error", message="缺乏访问权限 (HTTP 403)，非令牌失效")

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    token = str(data.get(TOKEN_KEY) or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Channel Access Token 为空")
    return _verify_line(token)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本，令牌只打码不回显。"""
    data = data or {}
    secret = str(data.get(TOKEN_KEY) or "").strip()
    if not secret:
        return NAME
    return f"{NAME} | {_mask(secret)}"
