"""DNSPod API Token 验证插件（plugins/dns/ 类别下的独立子插件）。"""
from dns_common import VerifyResult, body_snippet, guarded, make_client, safe_json

# DNSPod 明确的鉴权/账号类失败状态码：
# "-1" 登录失败、"-2" 需要实名认证、"-3" 缺少参数或 token 非法、
# "3" 域名不存在、"6" 无效的登录 token / 用户不存在、"85" 用户被封禁。
# 命中这些码即判定凭证无效，其余非 "1" 的状态码归入 error（无法确认）。
_DNSPOD_AUTH_FAIL_CODES = {"3", "6", "-1", "-2", "-3", "85"}


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_dnspod(data: dict) -> VerifyResult:
    """DNSPod：用 login_token 调用 Domain.List，检查 status.code。

    code == "1" 表示 Token 有效，并返回名下域名列表用于统计数量。
    """
    dp_id = (data.get("dp_id") or "").strip()
    dp_key = (data.get("dp_key") or "").strip()

    with make_client() as client:
        resp = client.post(
            "https://dnsapi.cn/Domain.List",
            data={
                "login_token": f"{dp_id},{dp_key}",
                "format": "json",
                "lang": "cn",
                "error_on_empty": "no",
            },
        )

    payload = safe_json(resp)
    if payload is None:
        # 非 JSON 响应（如网关错误页）视为无法确认
        return VerifyResult(
            status="error",
            message=f"响应非 JSON (HTTP {resp.status_code}): {body_snippet(resp)}",
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
        return _unconfirmed(resp.status_code, body_snippet(resp))

    detail = f"{api_message}" if api_message else f"code {code}"
    return VerifyResult(status="error", message=f"无法确认凭证状态 ({detail})")


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("dp_id") or "").strip():
        errors.append("DNSPod ID 不能为空")
    if not (data.get("dp_key") or "").strip():
        errors.append("DNSPod API Token 不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_dnspod(data)


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    dp_id = (data.get("dp_id") or "").strip()
    return f"DNSPod | ID: {dp_id}" if dp_id else "DNSPod"
