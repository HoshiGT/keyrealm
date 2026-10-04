"""域名解析类别共享模块：各服务验证处理器。

plugins/dns/ 下的每个子插件（dnspod、duckdns、godaddy）都是独立插件，
通过 `from dns_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持 DNSPod（腾讯云 DNS 解析）、DuckDNS（动态域名）、GoDaddy（域名注册商）。
本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


# 请求超时（秒）
TIMEOUT = 20.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


SERVICE_CONFIGS = {
    "dnspod": {"name": "DNSPod"},
    "duckdns": {"name": "DuckDNS"},
    "godaddy": {"name": "GoDaddy"},
}


# DNSPod 明确的鉴权/账号类失败状态码：
# "-1" 登录失败、"-2" 需要实名认证、"-3" 缺少参数或 token 非法、
# "3" 域名不存在、"6" 无效的登录 token / 用户不存在、"85" 用户被封禁。
# 命中这些码即判定凭证无效，其余非 "1" 的状态码归入 error（无法确认）。
_DNSPOD_AUTH_FAIL_CODES = {"3", "6", "-1", "-2", "-3", "85"}


def _log(message: str) -> None:
    print(f"[dns] {message}", file=sys.stderr)


def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _normalize_duckdns_domain(raw: str) -> str:
    """把用户填写的域名归一化为 DuckDNS 的 domains 参数（子域名部分）。

    允许 "xxx"、"xxx.duckdns.org"、"https://xxx.duckdns.org/" 等写法。
    """
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = value.split("//")[-1].split("/")[0].split("?")[0]
    suffix = ".duckdns.org"
    if value.endswith(suffix):
        value = value[: -len(suffix)]
    return value


# ── 各服务验证 ───────────────────────────────────────────

def _verify_dnspod(data: dict) -> VerifyResult:
    """DNSPod：用 login_token 调用 Domain.List，检查 status.code。

    code == "1" 表示 Token 有效，并返回名下域名列表用于统计数量。
    """
    dp_id = (data.get("dp_id") or "").strip()
    dp_key = (data.get("dp_key") or "").strip()

    with _client() as client:
        resp = client.post(
            "https://dnsapi.cn/Domain.List",
            data={
                "login_token": f"{dp_id},{dp_key}",
                "format": "json",
                "lang": "cn",
                "error_on_empty": "no",
            },
        )

    payload = _safe_json(resp)
    if payload is None:
        # 非 JSON 响应（如网关错误页）视为无法确认
        return VerifyResult(
            status="error",
            message=f"响应非 JSON (HTTP {resp.status_code}): {_body_snippet(resp)}",
        )

    status_info = payload.get("status")
    status_info = status_info if isinstance(status_info, dict) else {}
    code = str(status_info.get("code", "")).strip()
    api_message = str(status_info.get("message", "")).strip()

    if code == "1":
        info = payload.get("info")
        account_info = {"domain_count": len(info)} if isinstance(info, list) else None
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if code in _DNSPOD_AUTH_FAIL_CODES:
        message = f"凭证无效: {api_message}" if api_message else f"凭证无效 (code {code})"
        return VerifyResult(status="invalid", message=message)

    if not code:
        return _unconfirmed(resp.status_code, _body_snippet(resp))

    detail = f"{api_message}" if api_message else f"code {code}"
    return VerifyResult(status="error", message=f"无法确认凭证状态 ({detail})")


def _verify_duckdns(data: dict) -> VerifyResult:
    """DuckDNS：无独立校验端点，用自有域名调用一次 update 接口判定。

    注意：该请求会真实执行一次动态域名更新（txt 传空），属于对 DuckDNS 的
    写操作，仅在用户明确提供自有域名与 Token 时才应发起。
    响应正文首行为 "OK" 表示成功，"KO" 表示 Token 或域名错误。
    """
    token = (data.get("token") or "").strip()
    domain = _normalize_duckdns_domain(data.get("domain") or "")

    with _client() as client:
        resp = client.get(
            "https://www.duckdns.org/update",
            params={"domains": domain, "token": token, "txt": "", "verbose": "true"},
        )

    text = (resp.text or "").strip()
    # verbose=true 时 DuckDNS 会在状态行之后追加返回的 IP，因此只看首行
    status_line = text.splitlines()[0].strip().upper() if text else ""

    if status_line == "OK":
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info={"domain": f"{domain}.duckdns.org"},
        )
    if status_line == "KO":
        return VerifyResult(status="invalid", message="凭证无效：Token 或域名不正确")

    snippet = " ".join(text.split())[:_BODY_LIMIT]
    return VerifyResult(
        status="error",
        message=f"无法确认凭证状态 (HTTP {resp.status_code}): {snippet}",
    )


def _verify_godaddy(data: dict) -> VerifyResult:
    """GoDaddy：调用域名列表接口，检查 HTTP 状态码与返回列表。"""
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()

    with _client() as client:
        resp = client.get(
            "https://api.godaddy.com/v1/domains",
            params={"limit": 1},
            headers={
                "Authorization": f"sso-key {api_key}:{api_secret}",
                "Accept": "application/json",
            },
        )

    status = resp.status_code
    if status == 200:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        account_info = {"domain_count": len(payload)} if isinstance(payload, list) else None
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")

    return _unconfirmed(status, _body_snippet(resp))


_SERVICE_VERIFIERS = {
    "dnspod": _verify_dnspod,
    "duckdns": _verify_duckdns,
    "godaddy": _verify_godaddy,
}


# ── 子插件入口（service 由子插件显式传入） ───────────────

def validate_common(service: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = SERVICE_CONFIGS.get(service)
    if config is None:
        return [f"不支持的服务: {service}"]

    errors: list[str] = []

    if service == "dnspod":
        if not (data.get("dp_id") or "").strip():
            errors.append("DNSPod ID 不能为空")
        if not (data.get("dp_key") or "").strip():
            errors.append("DNSPod API Token 不能为空")
    elif service == "duckdns":
        if not (data.get("token") or "").strip():
            errors.append("DuckDNS Token 不能为空")
        if not (data.get("domain") or "").strip():
            errors.append("域名不能为空")
    elif service == "godaddy":
        if not (data.get("api_key") or "").strip():
            errors.append("GoDaddy API Key 不能为空")
        if not (data.get("api_secret") or "").strip():
            errors.append("GoDaddy API Secret 不能为空")

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的凭证是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")

        missing = validate_common(service, data)
        if missing:
            return VerifyResult(status="invalid", message="；".join(missing))

        return verifier(data)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        _log(f"verify 网络异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)

    if service == "dnspod":
        dp_id = (data.get("dp_id") or "").strip()
        return f"{name} | ID: {dp_id}" if dp_id else name

    if service == "duckdns":
        domain = _normalize_duckdns_domain(data.get("domain") or "")
        return f"{name} | {domain}.duckdns.org" if domain else name

    if service == "godaddy":
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return name
        masked = f"{api_key[:4]}..." if len(api_key) > 4 else "已配置"
        return f"{name} | API Key: {masked}"

    return name
