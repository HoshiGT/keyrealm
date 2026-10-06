"""Namecheap API 凭据验证插件（plugins_optin/dns/namecheap/，缺实例验证的仿写插件）。

端点与鉴权（来源见下）：
- 端点：``https://api.namecheap.com/xml.response``（HTTPS）。Namecheap 官方仓库
  ``namecheap/go-namecheap-sdk`` 内的 ``docs/namecheap-api-v2.md``（由官方方法文档生成）
  明确写 Production 为 ``https://api.namecheap.com/xml.response``，并注明「所有调用
  用 HTTP GET + query 参数」。沙箱为 ``https://api.sandbox.namecheap.com/xml.response``。
- 鉴权参数（每次调用必带）：``ApiUser`` / ``ApiKey`` / ``UserName`` / ``ClientIp``。
  ``UserName`` 通常与 ``ApiUser`` 相同；``ClientIp`` 是发起调用的服务器公网 IPv4，
  必须是你在 Namecheap 里加过白名单的 IP（走代理时填代理出口 IP）。
- 探测命令：``Command=namecheap.users.getBalances``（只读、幂等，返回账户余额）。

判定依据：
- 根节点 ``<ApiResponse Status="OK">`` → 有效；从
  ``CommandResponse/UserGetBalancesResult`` 取 ``AvailableBalance`` / ``Currency``
  作为余额附加信息（该结构已由官方仓库 ``users_get_balances.go`` 与
  ``fixtures/users_getBalances.xml`` 确认）。
- 根节点 ``Status="ERROR"`` 时读 ``<Errors><Error Number="...">文本</Error>``：
  - ``Number=1011150``（消息「Invalid request IP: …」）= 调用 IP 未加白名单 → ``error``
    （配置问题，不是凭据失效）。该编号出自官方仓库 namecheap/go-namecheap-sdk
    issue #4 的真实回包 ``<Error Number="1011150">Invalid request IP: 50.242.74.11</Error>``。
  - ``Number=1011102``（消息「API Key is invalid or API access has not been enabled」）=
    密钥无效（或账号未开 API 访问）→ ``invalid``。该编号 + 消息文本已由**本插件阴性对照
    直连生产 API 的真实回包**确认（见 tmp/probe_optin_dns.py 运行记录），判定时仍同时用
    「编号 + 消息文本」双保险。
  - 其余错误码一律 ``error``（拿不准绝不判有效）。

1011150 出自官方仓库真实回包；1011102 已由生产 API 阴性对照回包确认（不再存疑）。
仅官方错误码编号表整体未能逐字核对（官方 error-codes 页被 Cloudflare 拦截，curl / WebFetch 均 403）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from dns_common import VerifyResult, body_snippet, guarded, make_client

_ENDPOINT = "https://api.namecheap.com/xml.response"

# 「IP 未加白名单」的错误编号（官方仓库真实回包确认）。
_IP_NOT_WHITELISTED = "1011150"
# 「API Key 无效 / 未开通 API 访问」的错误编号（消息文本已证实，编号按任务给定）。
_KEY_INVALID = "1011102"


def _local(tag: str) -> str:
    """去掉 XML namespace 前缀，只留本地名（tag 形如 {ns}Error）。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_child(elem, name: str):
    """按本地名找第一个直接子节点，找不到返回 None。"""
    if elem is None:
        return None
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _first_error(root) -> tuple[str, str]:
    """从 <Errors><Error Number="...">文本</Error> 取第一个错误，返回 (编号, 文本)。"""
    errors = _find_child(root, "Errors")
    error = _find_child(errors, "Error")
    if error is None:
        return "", ""
    number = str(error.attrib.get("Number") or "").strip()
    text = " ".join((error.text or "").split())
    return number, text


def _to_float(value: str):
    """把余额字符串安全转成 float，非法则返回 None。"""
    try:
        number = float(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 也不收


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在消息里的密钥替换掉，任何路径都不回显 ApiKey。

    只替换长度 ≥ 6 的密钥（与 backend/api.py 的净化口径一致），否则短密钥会把
    消息里常见字符整片打码。
    """
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _mentions_ip(message: str) -> bool:
    """判断错误消息是否指向「调用 IP 未加白名单」。"""
    lowered = message.lower()
    return "invalid request ip" in lowered or "whitelist" in lowered or "not whitelisted" in lowered


def _verify_namecheap(data: dict) -> VerifyResult:
    """Namecheap：GET getBalances，按 XML 的 Status 与 Error Number 分类。"""
    api_key = (data.get("api_key") or "").strip()
    params = {
        "ApiUser": (data.get("api_user") or "").strip(),
        "ApiKey": api_key,
        "UserName": (data.get("username") or "").strip(),
        "ClientIp": (data.get("client_ip") or "").strip(),
        "Command": "namecheap.users.getBalances",
    }

    with make_client() as client:
        resp = client.get(_ENDPOINT, params=params)

    body = resp.text or ""
    try:
        root = ET.fromstring(body)
    except Exception:  # noqa: BLE001 - 非 XML（网关错误页等）
        return VerifyResult(
            status="error",
            message=f"响应非 XML (HTTP {resp.status_code}): "
                    f"{_redact(body_snippet(resp), api_key)}",
        )

    status_attr = root.attrib.get("Status")
    if status_attr == "OK":
        account_info = None
        command_response = _find_child(root, "CommandResponse")
        result = _find_child(command_response, "UserGetBalancesResult")
        if result is not None:
            currency = (result.attrib.get("Currency") or "").strip()
            balance = _to_float(result.attrib.get("AvailableBalance"))
            if balance is not None and currency:
                account_info = {"balance": balance, "currency": currency}
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if status_attr == "ERROR":
        number, message = _first_error(root)
        message = _redact(message, api_key)
        if number == _IP_NOT_WHITELISTED or _mentions_ip(message):
            detail = message or f"code {number}"
            return VerifyResult(status="error", message=f"调用 IP 未加白名单（{detail}）")
        if number == _KEY_INVALID or "API Key is invalid" in message:
            detail = message or f"code {number}"
            return VerifyResult(status="invalid", message=f"凭证无效（{detail}）")
        detail = message or f"code {number}" if number else body_snippet(resp)
        return VerifyResult(status="error", message=f"无法确认凭证状态（{detail}）")

    return VerifyResult(status="error", message=f"无法确认凭证状态 (HTTP {resp.status_code})")


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_user") or "").strip():
        errors.append("Namecheap ApiUser 不能为空")
    if not (data.get("api_key") or "").strip():
        errors.append("Namecheap ApiKey 不能为空")
    if not (data.get("username") or "").strip():
        errors.append("Namecheap UserName 不能为空")
    if not (data.get("client_ip") or "").strip():
        errors.append("Namecheap ClientIp 不能为空（必须是已加白名单的公网 IP，走代理时填出口 IP）")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_namecheap(data)


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显 ApiKey）。"""
    data = data or {}
    api_user = (data.get("api_user") or "").strip()
    return f"Namecheap | {api_user}" if api_user else "Namecheap"
