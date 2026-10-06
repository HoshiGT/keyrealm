"""Azure 服务主体（Service Principal）凭据校验插件。

端点
    POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
鉴权
    OAuth2 client_credentials 流程：表单 grant_type=client_credentials + client_id +
    client_secret，scope=https://management.azure.com/.default。
判定依据
    - 缺字段                     -> invalid（凭据本身不完整，坏凭据）
    - HTTP 200 且带 access_token -> valid（account_info 只放非敏感的 token_expires_in）
    - HTTP 404                   -> invalid（租户 ID 不存在）
    - error == invalid_client / unauthorized_client -> invalid（客户端 ID 或密码错）
    - error_description 含 AADSTS 前缀错误码（如 AADSTS7000215 密码错、700016 应用不存在）-> invalid
    - error == invalid_request / invalid_scope -> error（请求构造问题，不证明凭据坏）
    - 其它状态码 / 解析不出 JSON    -> error（拿不准，绝不判 valid）

只调用 /oauth2/v2.0/token 换 token，不再用 token 调用任何 ARM 资源 API（如
management.azure.com/subscriptions），避免对账号产生多余调用痕迹。

参考文档：https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
"""

import re

from cloud_common import VerifyResult, guarded, make_client, safe_json, body_snippet

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
SCOPE = "https://management.azure.com/.default"

REQUIRED = (
    ("tenant_id", "租户 ID"),
    ("client_id", "客户端 ID"),
    ("client_secret", "客户端密码"),
)

# 明确证明「凭据坏掉」的错误码
_CREDENTIAL_ERRORS = {"invalid_client", "unauthorized_client"}
# 请求构造问题：不证明凭据坏，报 error
_REQUEST_ERRORS = {"invalid_request", "invalid_scope"}


def _clean_detail(text):
    """裁掉 Azure 错误描述里常带的 Trace ID / Correlation ID / Timestamp 尾巴。"""
    text = (text or "").strip()
    if not text:
        return ""
    text = re.split(r"\s*Trace ID:|\s*Correlation ID:|\s*Timestamp:", text)[0].strip()
    return text[:240]


@guarded
def verify(data: dict):
    tenant_id = (data.get("tenant_id") or "").strip()
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()

    missing = [label for key, label in REQUIRED if not data.get(key)]
    if missing:
        return VerifyResult(status="invalid", message="缺少必填字段：" + "、".join(missing))

    url = TOKEN_URL.format(tenant_id=tenant_id)
    try:
        resp = make_client().post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": SCOPE,
            },
        )
    except Exception as exc:  # guarded 兜底，此处只为拼出友好消息
        return VerifyResult(status="error", message="请求异常：%s" % exc)

    payload = safe_json(resp) or {}

    if resp.status_code == 200 and payload.get("access_token"):
        return VerifyResult(
            status="valid",
            message="服务主体有效",
            account_info={"token_expires_in": payload.get("expires_in", "")},
        )

    if resp.status_code == 404:
        return VerifyResult(status="invalid", message="租户 ID 不存在（HTTP 404）")

    error = (payload.get("error") or "").strip()
    desc = _clean_detail(payload.get("error_description"))

    if error in _CREDENTIAL_ERRORS:
        detail = desc or error
        return VerifyResult(status="invalid", message="服务主体无效（%s）：%s" % (error, detail))

    if "AADSTS" in (desc or "") or "AADSTS" in (payload.get("error_description") or ""):
        detail = desc or payload.get("error_description")
        return VerifyResult(status="invalid", message="服务主体无效（AADSTS）：%s" % detail)

    if error in _REQUEST_ERRORS:
        detail = desc or error
        return VerifyResult(status="error", message="请求被拒绝（%s）：%s" % (error, detail))

    if error:
        detail = desc or error
        return VerifyResult(status="error", message="无法确认凭证状态（%s）：%s" % (error, detail))

    return VerifyResult(
        status="error",
        message="无法确认凭证状态（HTTP %s）：%s" % (resp.status_code, body_snippet(resp)),
    )


def validate(data) -> list[str]:
    """非空校验，返回错误列表（空列表=通过）；不保证凭据有效。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append("%s不能为空" % label)
    return errors


def get_display_summary(data: dict) -> str:
    tenant_id = (data.get("tenant_id") or "").strip()
    client_id = (data.get("client_id") or "").strip()
    return "Azure 服务主体 %s / %s" % (tenant_id[:24], client_id[:24])
