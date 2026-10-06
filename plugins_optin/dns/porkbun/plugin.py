"""Porkbun API 凭据验证插件（plugins_optin/dns/porkbun/，缺实例验证的仿写插件）。

端点与鉴权（来源见下）：
- 端点：``https://api.porkbun.com/api/json/v3/ping``（HTTPS）。官方 README
  （porkbun.com 的 API 文档首页给出的指引）明确写 POST + ``Content-Type: application/json``，
  JSON body 带 ``apikey`` 与 ``secretapikey``。基础地址 ``https://api.porkbun.com/api/json/v3``。
- 探测命令：``/ping``（只读、幂等，成功时返回 ``status=SUCCESS`` 与 ``yourIp``）。

判定依据：
- ``{"status": "SUCCESS", "yourIp": "..."}`` → 有效；取 ``yourIp`` 作为附加信息。
- ``{"status": "ERROR", ...}``：按 message 分类（实际阴性对照回包为 HTTP 400 +
  ``{"status":"ERROR","message":"Invalid API key.","code":"INVALID_API_KEYS","next_action":{...}}``，
  与官方 README 示例的措辞略有出入，故按消息关键词而非固定文案判定）：
  - 消息含 invalid api key / secret 等字样 → ``invalid``。
  - 消息含 2FA / two-factor → ``error``。
- HTTP 403：官方 README 注明「403 Forbidden 可能表示需要额外认证（如两步验证）」，
  因此 403 一律 ``error``（账号状态问题，不是密钥本身错误）。
- 其余（非 JSON、非 SUCCESS/ERROR、其它错误消息）一律 ``error``（拿不准绝不判有效）。

参考文档：
- https://porkbun.com/api/json/v3/documentation （官方 API 文档，含鉴权与错误处理）
"""
from __future__ import annotations

from dns_common import VerifyResult, body_snippet, guarded, make_client, safe_json

_ENDPOINT = "https://api.porkbun.com/api/json/v3/ping"


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在消息里的密钥替换掉，任何路径都不回显 secretapikey。

    只替换长度 ≥ 6 的密钥，否则短密钥会把消息里常见字符整片打码。
    """
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _mentions_2fa(message: str) -> bool:
    lowered = (message or "").lower()
    return "two-factor" in lowered or "two factor" in lowered or "2fa" in lowered


def _mentions_invalid_key(message: str) -> bool:
    lowered = (message or "").lower()
    return "invalid api key" in lowered or "invalid api" in lowered or "invalid secret" in lowered


def _verify_porkbun(data: dict) -> VerifyResult:
    """Porkbun：POST /ping，按 JSON 的 status 与消息分类。"""
    secret = (data.get("secretapikey") or "").strip()
    payload = {
        "apikey": (data.get("apikey") or "").strip(),
        "secretapikey": secret,
    }

    with make_client() as client:
        resp = client.post(_ENDPOINT, json=payload)

    if resp.status_code == 403:
        return VerifyResult(
            status="error",
            message="HTTP 403：Porkbun 可能要求额外认证（如两步验证），无法确认密钥状态",
        )

    body = safe_json(resp)
    if body is None:
        return VerifyResult(
            status="error",
            message=f"响应非 JSON (HTTP {resp.status_code}): "
                    f"{_redact(body_snippet(resp), secret)}",
        )

    status = str(body.get("status") or "").upper()
    if status == "SUCCESS":
        your_ip = body.get("yourIp")
        account_info = {"yourIp": str(your_ip)} if your_ip else None
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if status == "ERROR":
        message = _redact(str(body.get("message") or ""), secret)
        if _mentions_2fa(message):
            return VerifyResult(status="error", message=f"需要额外认证（{message}）")
        if _mentions_invalid_key(message):
            return VerifyResult(status="invalid", message=f"凭证无效（{message}）")
        return VerifyResult(status="error", message=f"无法确认凭证状态（{message}）")

    return VerifyResult(
        status="error",
        message=f"无法确认凭证状态 (HTTP {resp.status_code})",
    )


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("apikey") or "").strip():
        errors.append("Porkbun API Key 不能为空")
    if not (data.get("secretapikey") or "").strip():
        errors.append("Porkbun Secret API Key 不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_porkbun(data)


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显 Secret API Key）。"""
    data = data or {}
    apikey = (data.get("apikey") or "").strip()
    if not apikey:
        return "Porkbun"
    mask = apikey[:4] + "***" if len(apikey) > 4 else "***"
    return f"Porkbun | {mask}"
