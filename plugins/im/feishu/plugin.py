"""飞书应用凭证验证插件（plugins/im/ 类别下的独立子插件）。

端点：POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
自建/商店应用用 app_id + app_secret 换取 tenant_access_token；
成功时返回 token，失败时 HTTP 200 + code != 0
（常见错误码：10003 app_id 不存在、10007、99991663 app_id/secret 不匹配）。
"""
from im_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE = "feishu"
NAME = "飞书应用"
REQUIRED = (("app_id", "App ID"), ("app_secret", "App Secret"))
DISPLAY_FIELD = "app_id"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _error_detail(payload) -> str:
    """尽力从错误响应中提取可读原因（飞书/钉钉/QQ 均有 message 类字段）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "errmsg", "description", "error_description"):
        value = payload.get(key)
        if value:
            return str(value)
    code = payload.get("code")
    if code is None:
        code = payload.get("errcode")
    return f"错误码 {code}" if code is not None else ""


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    return errors


def _verify(data: dict) -> VerifyResult:
    app_id = (data.get("app_id") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"app_id": app_id, "app_secret": app_secret},
        )
    status = resp.status_code
    payload = safe_json(resp)
    if isinstance(payload, dict):
        code = payload.get("code")
        if code in (0, "0"):
            return VerifyResult(status="valid", message="凭证有效")
        if code is not None:
            detail = _error_detail(payload)
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify(data)


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
