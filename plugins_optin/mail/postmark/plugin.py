"""Postmark 密钥验证插件（plugins_optin/mail/postmark/，自包含实现）。

端点：GET https://api.postmarkapp.com/server
鉴权：请求头 ``X-Postmark-Server-Token: <Server Token>``，并带 ``Accept: application/json``

判定依据：
  - 200  → valid，account_info 放 Name / ID
  - 401  → invalid（官方文档明确：401 = 缺少或错误的 token，error code 10）
  - 422  → error（官方：422 是「JSON 格式/字段不合法」，与鉴权无关，故不判 invalid）
  - 403 / 429 / 其它状态码 / 网络异常 → error（拿不准，绝不报 valid）

已查官方文档确认：
  - 端点与鉴权头：Postmark「Server API」——GET https://api.postmarkapp.com/server，
    头 ``X-Postmark-Server-Token``（server 级权限，取自 API Tokens 页签）与 ``Accept: application/json``。
    https://postmarkapp.com/developer/api/server-api
  - 无效 token 语义：Postmark「Overview」——401 = Missing or incorrect API token（error code 10）；
    422 用于 malformed JSON / invalid fields（非鉴权）。https://postmarkapp.com/developer/api/overview
存疑/未经确认：422 在本端点（GET 无请求体）下出现的具体场景未实测；按官方文档归为非鉴权 → error。

本插件只做只读探测，绝不调用发信接口（/email 等）。

参考文档：https://postmarkapp.com/developer/api/server-api
参考文档：https://postmarkapp.com/developer/api/overview
"""
from __future__ import annotations

import httpx

from mail_common import VerifyResult, guarded, log, proxy, timeout

NAME = "Postmark"
ENDPOINT = "https://api.postmarkapp.com/server"


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
    if not (data.get("server_token") or "").strip():
        return ["Server Token 不能为空"]
    return []


def _verify(data: dict) -> VerifyResult:
    token = (data.get("server_token") or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="Server Token 为空")

    headers = {"X-Postmark-Server-Token": token, "Accept": "application/json"}
    try:
        with _make_client() as client:
            resp = client.get(ENDPOINT, headers=headers)
    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(timeout())} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")

    if resp.status_code == 200:
        payload = _safe_json(resp) or {}
        info = {}
        if isinstance(payload.get("Name"), str) and payload["Name"]:
            info["name"] = payload["Name"]
        if payload.get("ID") is not None:
            info["id"] = str(payload["ID"])
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效：Server Token 错误 (HTTP 401)")
    if resp.status_code == 429:
        return VerifyResult(status="error", message="触发速率限制 (HTTP 429)，无法确认凭证状态")
    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 Postmark Server Token 是否有效。保证不抛出异常（guarded 兜底）。"""
    data = data or {}
    log(f"校验 {NAME} 凭据")
    return _verify(data)


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（token 打码，绝不回显明文）。"""
    data = data or {}
    return f"{NAME} | Server Token: {_mask(data.get('server_token'))}"
