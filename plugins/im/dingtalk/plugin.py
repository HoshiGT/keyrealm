"""钉钉应用凭证验证插件（plugins/im/ 类别下的独立子插件）。

企业内部应用，两条端点、按顺序尝试：
1. 新版 POST https://api.dingtalk.com/v1.0/oauth2/accessToken
   （appKey/appSecret 走 JSON body）；
2. 新版不可用（404/405、非 JSON、5xx、网络异常）时回退旧版
   GET https://oapi.dingtalk.com/gettoken?appkey=..&appsecret=..
   注意：旧版 appsecret 走查询串，属钉钉旧版设计，该 URL 不可透出到消息里。
"""
import httpx

from im_common import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    safe_json,
)

SERVICE = "dingtalk"
NAME = "钉钉"
REQUIRED = (("app_key", "App Key"), ("app_secret", "App Secret"))
DISPLAY_FIELD = "app_key"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _safe_request_error(exc: Exception) -> VerifyResult:
    """网络异常兜底：只暴露异常类型（旧版端点密钥在查询串里）。"""
    return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")


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


def _oauth2(app_key: str, app_secret: str):
    """POST /v1.0/oauth2/accessToken；端点不可用（需回退）时返回 None。"""
    try:
        with make_client() as client:
            resp = client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                headers={"Content-Type": "application/json"},
                json={"appKey": app_key, "appSecret": app_secret},
            )
    except httpx.HTTPError as exc:
        log(f"钉钉新版端点请求失败，回退旧版: {type(exc).__name__}")
        return None

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        if isinstance(payload, dict) and payload.get("accessToken"):
            return VerifyResult(status="valid", message="凭证有效")
        detail = _error_detail(payload) or body_snippet(resp)
        return VerifyResult(status="invalid", message=f"凭证无效: {detail}" if detail else "凭证无效")
    if status in (400, 401, 403):
        detail = _error_detail(payload) or body_snippet(resp)
        return VerifyResult(
            status="invalid",
            message=f"凭证无效: {detail}" if detail else f"凭证无效 (HTTP {status})",
        )
    if status in (404, 405):
        return None
    log(f"钉钉新版端点异常响应 HTTP {status}，回退旧版")
    return None


def _gettoken(app_key: str, app_secret: str) -> VerifyResult:
    """旧版 GET /gettoken?appkey=..&appsecret=..（appsecret 走查询串，属钉钉旧版设计）。"""
    try:
        with make_client() as client:
            resp = client.get(
                "https://oapi.dingtalk.com/gettoken",
                params={"appkey": app_key, "appsecret": app_secret},
            )
    except httpx.HTTPError as exc:
        return _safe_request_error(exc)

    status = resp.status_code
    payload = safe_json(resp)
    if isinstance(payload, dict):
        errcode = payload.get("errcode")
        if errcode in (0, "0") and payload.get("access_token"):
            return VerifyResult(status="valid", message="凭证有效")
        if errcode is not None:
            detail = _error_detail(payload)
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


def _verify(data: dict) -> VerifyResult:
    app_key = (data.get("app_key") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()

    result = _oauth2(app_key, app_secret)
    if result is not None:
        return result
    # 新版端点不可用（404/405、非 JSON、5xx 等）→ 回退旧版
    return _gettoken(app_key, app_secret)


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
