"""Mailgun 密钥验证插件（plugins_optin/mail/mailgun/，自包含实现）。

端点：GET {base}/domains（base 按 region 取 US/EU 主机）
鉴权：HTTP Basic Auth，用户名固定 ``api``、密码为账户级 Private API Key

判定依据：
  - 200  → valid，account_info 放域名条数（total_count / items 数量）
  - 401  → invalid（Basic 鉴权失败）
  - 403 / 429 / 其它状态码 / 网络异常 → error（拿不准，绝不报 valid）

已查官方文档确认：
  - 鉴权：Mailgun「Authentication」页——HTTP Basic Auth，username ``api``、password 为
    Private API Key。
  - 主机与 domains 端点：US ``api.mailgun.net``、EU ``api.eu.mailgun.net``；官方文档
    「Get domains」页现列 GET /v4/domains，本插件沿用仍可用的 /v3/domains（US/EU 两区域
    经代理实测均返回 401 "Invalid private key"，鉴权语义正确）。
存疑/未经确认：官方文档主推 /v4/domains（分页 limit/skip），v3 未在新文档单列；
    /v3/domains 为长期稳定端点，实测可用。若未来下线需改走 /v4/domains 并适配响应结构。

本插件只做只读探测，绝不调用发信接口（/messages 等）。

参考文档：https://documentation.mailgun.com/docs/mailgun/api-reference/mg-auth
参考文档：https://documentation.mailgun.com/docs/mailgun/api-reference/send/mailgun/domains/get-v4-domains
"""
from __future__ import annotations

import httpx

from mail_common import VerifyResult, guarded, log, proxy, timeout

NAME = "Mailgun"
US_BASE = "https://api.mailgun.net/v3"
EU_BASE = "https://api.eu.mailgun.net/v3"


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


def _base_url(data: dict) -> str:
    region = (data.get("region") or "us").strip().lower()
    return EU_BASE if region == "eu" else US_BASE


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("Private API Key 不能为空")
    region = (data.get("region") or "us").strip().lower()
    if region not in ("us", "eu"):
        errors.append("区域只能选 US 或 EU")
    return errors


def _verify(data: dict) -> VerifyResult:
    key = (data.get("api_key") or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="Private API Key 为空")

    url = _base_url(data) + "/domains"
    try:
        with _make_client() as client:
            resp = client.get(url, auth=("api", key))
    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(timeout())} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")

    if resp.status_code == 200:
        payload = _safe_json(resp) or {}
        items = payload.get("items")
        items = items if isinstance(items, list) else []
        total = payload.get("total_count")
        domain_count = total if isinstance(total, int) else len(items)
        return VerifyResult(status="valid", message="凭证有效", account_info={"domain_count": domain_count})
    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效：API Key 错误 (HTTP 401)")
    if resp.status_code == 429:
        return VerifyResult(status="error", message="触发速率限制 (HTTP 429)，无法确认凭证状态")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 Mailgun Private API Key 是否有效。保证不抛出异常（guarded 兜底）。"""
    data = data or {}
    log(f"校验 {NAME} 凭据（区域 {data.get('region') or 'us'}）")
    return _verify(data)


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（密钥打码，绝不回显明文）。"""
    data = data or {}
    region = (data.get("region") or "us").strip().lower()
    region_label = "EU" if region == "eu" else "US"
    domain = (data.get("domain") or "").strip()
    summary = f"{NAME} ({region_label}) | API Key: {_mask(data.get('api_key'))}"
    return f"{summary} | {domain}" if domain else summary
