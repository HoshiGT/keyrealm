"""Brevo 密钥验证插件（plugins_optin/mail/brevo/，自包含实现）。

端点：GET https://api.brevo.com/v3/account
鉴权：请求头 ``api-key: <API Key>``（`xkeysib-` 开头；注意是连字符 ``api-key``，不是 ``api_key``）

判定依据：
  - 200  → valid，account_info 放 email / plan
  - 400 且响应 ``code`` 命中鉴权类关键字（unauthorized / api-key / authentication 等）→ invalid
    —— Brevo 官方文档对无效 Key 返回的是 400 而非 401（code 枚举含 ``unauthorized``、
    ``api-key not found``、``Authentication failed``）
  - 401 / 403 → invalid（通用兜底，以防文档未列出的旧行为）
  - 429 / 其它状态码 / 网络异常 → error（拿不准，绝不报 valid）

已查官方文档确认：
  - 端点与鉴权头：Brevo Developers「Get account details」——GET https://api.brevo.com/v3/account，
    头 ``api-key``（文档原文「The API key should be passed in the request headers as api-key」）。
    https://developers.brevo.com/reference/getaccount
  - 无效 Key 状态码：同上页只列 200 / 400，code 枚举含 unauthorized / api-key not found /
    Authentication failed，即无效 Key 以 400 形式返回。
存疑/未经确认：400 的 code 具体取值（历史演进过，含 ``unauthorized`` 与
    ``invalid_parameter`` 等）；故判定只认「code 含鉴权关键字」，其余 400 一律 error。

本插件只做只读探测，绝不调用发信接口（/v3/smtp/email 等）。

参考文档：https://developers.brevo.com/reference/getaccount
"""
from __future__ import annotations

import httpx

from mail_common import VerifyResult, guarded, log, proxy, timeout

NAME = "Brevo"
ENDPOINT = "https://api.brevo.com/v3/account"

# 400 响应 code 里出现这些关键字才判定为「鉴权失败」；其余 400（参数错误等）判 error
_AUTH_CODE_HINTS = ("unauthorized", "api-key", "api_key", "authentication", "invalid key", "key not found")


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


def _is_auth_failure(payload: dict) -> bool:
    """判断 400 响应是否属于鉴权失败（而不是其它参数错误）。"""
    code = str(payload.get("code") or "").lower()
    message = str(payload.get("message") or "").lower()
    return any(hint in code or hint in message for hint in _AUTH_CODE_HINTS)


def _verify(data: dict) -> VerifyResult:
    key = (data.get("api_key") or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="API Key 为空")

    try:
        with _make_client() as client:
            resp = client.get(ENDPOINT, headers={"api-key": key})
    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(timeout())} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")

    if resp.status_code == 200:
        payload = _safe_json(resp) or {}
        info = {}
        if isinstance(payload.get("email"), str) and payload["email"]:
            info["email"] = payload["email"]
        if isinstance(payload.get("plan"), list) and payload["plan"]:
            # plan 是数组（含 type/credits 等对象），只取第一个的 type 便于展示
            first = payload["plan"][0] if isinstance(payload["plan"][0], dict) else None
            if first and first.get("type"):
                info["plan"] = str(first["type"])
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if resp.status_code == 400:
        payload = _safe_json(resp) or {}
        if _is_auth_failure(payload):
            code = payload.get("code") or "unauthorized"
            return VerifyResult(status="invalid", message=f"凭证无效 (HTTP 400: {code})")
        return VerifyResult(status="error", message="无法确认凭证状态 (HTTP 400)")
    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    if resp.status_code == 429:
        return VerifyResult(status="error", message="触发速率限制 (HTTP 429)，无法确认凭证状态")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 Brevo API Key 是否有效。保证不抛出异常（guarded 兜底）。"""
    data = data or {}
    log(f"校验 {NAME} 凭据")
    return _verify(data)


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（密钥打码，绝不回显明文）。"""
    data = data or {}
    return f"{NAME} | API Key: {_mask(data.get('api_key'))}"
