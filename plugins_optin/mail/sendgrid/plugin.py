"""SendGrid 密钥验证插件（plugins_optin/mail/sendgrid/，自包含实现）。

端点：GET https://api.sendgrid.com/v3/scopes
鉴权：Authorization: Bearer <API Key>（`SG.` 开头的 SendGrid API Key）

判定依据：
  - 200  → valid（响应含 ``scopes`` 数组，即该 Key 的权限列表），account_info 放 scope 数量
  - 401/403 → invalid（官方文档明确这两类为鉴权 / 授权失败）
  - 429 / 其它状态码 / 网络异常 → error（拿不准，绝不报 valid）

已查官方文档确认：
  - 端点与鉴权：Twilio SendGrid API Reference「Retrieve a list of scopes for which
    this user has access」（GET /v3/scopes，Bearer），200 返回 ``{"scopes": [...]}``，
    另列 401（authorization required）/ 403（access forbidden）/ 404 / 500。

本插件只做只读探测，绝不调用发信接口（/mail/send 等）。

参考文档：https://www.twilio.com/docs/sendgrid/api-reference/api-key-permissions/retrieve-a-list-of-scopes-for-which-this-user-has-access
"""
from __future__ import annotations

import httpx

from mail_common import VerifyResult, guarded, log, proxy, timeout

NAME = "SendGrid"
ENDPOINT = "https://api.sendgrid.com/v3/scopes"


# ── 本地 HTTP 辅助 ─────────────────────────────────────────
# mail_common 目前只提供 SMTP/IMAP 的 TCP 隧道工具，不含 make_client / safe_json，
# 故在此按 api_common 同款实现，复用 mail_common.proxy() / timeout()，保持「HTTP 走代理」。
def _make_client() -> httpx.Client:
    return httpx.Client(timeout=timeout(), follow_redirects=True, proxy=proxy())


def _safe_json(resp: httpx.Response):
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应体不回显
        return None
    return data if isinstance(data, dict) else None


def _mask(secret: str) -> str:
    """把密钥打码成展示文本；短密钥整体 ***，绝不回显明文。"""
    secret = (secret or "").strip()
    if not secret:
        return "未配置"
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "…" + secret[-4:]


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    if not (data.get("api_key") or "").strip():
        return ["API Key 不能为空"]
    return []


def _verify(data: dict) -> VerifyResult:
    key = (data.get("api_key") or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="API Key 为空")

    try:
        with _make_client() as client:
            resp = client.get(ENDPOINT, headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(timeout())} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")

    if resp.status_code == 200:
        payload = _safe_json(resp) or {}
        scopes = payload.get("scopes")
        count = len(scopes) if isinstance(scopes, list) else 0
        return VerifyResult(status="valid", message="凭证有效", account_info={"scope_count": count})
    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    if resp.status_code == 429:
        return VerifyResult(status="error", message="触发速率限制 (HTTP 429)，无法确认凭证状态")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 SendGrid API Key 是否有效。保证不抛出异常（guarded 兜底）。"""
    data = data or {}
    log(f"校验 {NAME} 凭据")
    return _verify(data)


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（密钥打码，绝不回显明文）。"""
    data = data or {}
    return f"{NAME} | API Key: {_mask(data.get('api_key'))}"
