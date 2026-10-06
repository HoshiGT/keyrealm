"""Backblaze B2 应用密钥校验插件。

端点
    GET https://api.backblazeb2.com/b2api/v4/b2_authorize_account
鉴权
    HTTP Basic：Authorization: Basic base64("{key_id}:{application_key}")。
判定依据
    - 缺字段       -> invalid（凭据本身不完整，坏凭据）
    - HTTP 200     -> valid（account_info 取非敏感的 account_id←accountId、
                      可选的 key_expiration←applicationKeyExpirationTimestamp）
    - HTTP 401     -> invalid（官方返回 code "unauthorized"）
    - 其它状态码    -> error（拿不准，绝不判 valid）

注意：
    - 绝不放、绝不回显 authorizationToken（它等价于临时访问令牌，属敏感信息）。
    - 版本选用 v4（2025-04-24 起为当前版本）：v4 兼容 v3 密钥，且多桶（multi-bucket）
      应用密钥只被 v4 接受、会被 v1-v3 拒绝。主密钥与单桶密钥在 v3/v4 均可用。

参考文档：https://www.backblaze.com/apidocs/b2-authorize-account
"""

import base64

from cloud_common import VerifyResult, guarded, make_client, safe_json, body_snippet

AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"

REQUIRED = (
    ("key_id", "Key ID"),
    ("application_key", "Application Key"),
)


@guarded
def verify(data: dict):
    key_id = (data.get("key_id") or "").strip()
    application_key = (data.get("application_key") or "").strip()

    missing = [label for key, label in REQUIRED if not data.get(key)]
    if missing:
        return VerifyResult(status="invalid", message="缺少必填字段：" + "、".join(missing))

    raw = ("%s:%s" % (key_id, application_key)).encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")

    try:
        resp = make_client().get(
            AUTHORIZE_URL,
            headers={"Authorization": "Basic %s" % token},
        )
    except Exception as exc:
        return VerifyResult(status="error", message="请求异常：%s" % exc)

    payload = safe_json(resp) or {}

    if resp.status_code == 200 and payload.get("accountId"):
        info = {"account_id": payload.get("accountId", "")}
        exp = payload.get("applicationKeyExpirationTimestamp")
        if exp:
            info["key_expiration"] = exp
        return VerifyResult(
            status="valid",
            message="Backblaze B2 应用密钥有效",
            account_info=info,
        )

    if resp.status_code == 401:
        return VerifyResult(status="invalid", message="Backblaze B2 应用密钥无效（HTTP 401）")

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
    key_id = (data.get("key_id") or "").strip()
    return "Backblaze B2 %s" % key_id[:24]
