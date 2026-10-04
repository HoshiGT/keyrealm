"""GoDaddy API Key 验证插件（plugins/dns/ 类别下的独立子插件）。"""
from dns_common import VerifyResult, body_snippet, guarded, make_client


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_godaddy(data: dict) -> VerifyResult:
    """GoDaddy：调用域名列表接口，检查 HTTP 状态码与返回列表。"""
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()

    with make_client() as client:
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

    return _unconfirmed(status, body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("GoDaddy API Key 不能为空")
    if not (data.get("api_secret") or "").strip():
        errors.append("GoDaddy API Secret 不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_godaddy(data)


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return "GoDaddy"
    masked = f"{api_key[:4]}..." if len(api_key) > 4 else "已配置"
    return f"GoDaddy | API Key: {masked}"
