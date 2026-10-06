"""Resend 密钥验证插件（plugins_optin/mail/resend/，自包含实现）。

端点：GET https://api.resend.com/domains
鉴权：Authorization: Bearer <API Key>（`re_` 开头的 Resend API Key）

判定依据：
  - 200  → valid（响应含 ``data`` 数组），account_info 放域名条数
  - 400 且 ``message`` 命中鉴权关键字（api key / invalid / unauthorized / authentication）→ invalid
    —— 实测：无效 key 返回 400，body ``{"statusCode":400,"message":"API key is invalid","name":"validation_error"}``
  - 401  → invalid（通用兜底）
  - 其它 400（真正的参数错误）/ 403 / 429 / 其它状态码 / 网络异常 → error（拿不准，绝不报 valid）

已查官方文档确认：
  - 端点与鉴权：Resend API Reference「List domains」——GET https://api.resend.com/domains，
    Bearer（``Authorization: Bearer re_xxxx``）。见
    https://resend.com/docs/api-reference/domains/list-domains
经代理阴性对照实测（乱写 key）：
  - 真实返回 400 ``"API key is invalid"``（非 401），故 400 需按 message 判鉴权失败。

本插件只做只读探测，绝不调用发信接口（/emails 等）。

参考文档：https://resend.com/docs/api-reference/domains/list-domains
"""
from __future__ import annotations

import httpx

from mail_common import VerifyResult, guarded, log, proxy, timeout

NAME = "Resend"
ENDPOINT = "https://api.resend.com/domains"


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
        rows = payload.get("data")
        rows = rows if isinstance(rows, list) else []
        return VerifyResult(status="valid", message="凭证有效", account_info={"domain_count": len(rows)})
    if resp.status_code == 400:
        payload = _safe_json(resp) or {}
        message = str(payload.get("message") or "").lower()
        if any(h in message for h in ("api key", "invalid", "unauthorized", "authentication")):
            return VerifyResult(status="invalid", message="凭证无效 (HTTP 400)")
        return VerifyResult(status="error", message="无法确认凭证状态 (HTTP 400)")
    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP 401)")
    if resp.status_code == 429:
        return VerifyResult(status="error", message="触发速率限制 (HTTP 429)，无法确认凭证状态")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 Resend API Key 是否有效。保证不抛出异常（guarded 兜底）。"""
    data = data or {}
    log(f"校验 {NAME} 凭据")
    return _verify(data)


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（密钥打码，绝不回显明文）。"""
    data = data or {}
    return f"{NAME} | API Key: {_mask(data.get('api_key'))}"
