"""DigitalOcean 个人访问令牌校验插件。

端点
    GET https://api.digitalocean.com/v2/account
鉴权
    HTTP Bearer：Authorization: Bearer {token}。
判定依据
    - 缺 token       -> invalid（凭据本身不完整，坏凭据）
    - HTTP 200       -> valid（account_info 取非敏感的 email / status / droplet_limit / uuid）
    - HTTP 401       -> invalid（官方返回 "Unable to authenticate you."）
    - 其它状态码      -> error（拿不准，绝不判 valid）

account_info 只放账号非敏感元信息（email、status、droplet_limit、account_id←uuid），
绝不放 token。

参考文档：https://docs.digitalocean.com/reference/api/reference/account/
"""

from cloud_common import VerifyResult, guarded, make_client, safe_json, body_snippet

ACCOUNT_URL = "https://api.digitalocean.com/v2/account"


@guarded
def verify(data: dict):
    token = (data.get("token") or "").strip()
    if not token:
        return VerifyResult(status="invalid", message="缺少 API Token")

    try:
        resp = make_client().get(
            ACCOUNT_URL,
            headers={"Authorization": "Bearer %s" % token},
        )
    except Exception as exc:
        return VerifyResult(status="error", message="请求异常：%s" % exc)

    if resp.status_code == 200:
        account = safe_json(resp) or {}
        account = account.get("account") or account
        return VerifyResult(
            status="valid",
            message="DigitalOcean Token 有效",
            account_info={
                "email": account.get("email", ""),
                "status": account.get("status", ""),
                "droplet_limit": account.get("droplet_limit", ""),
                "account_id": account.get("uuid", ""),
            },
        )

    if resp.status_code == 401:
        return VerifyResult(status="invalid", message="DigitalOcean Token 无效（HTTP 401）")

    return VerifyResult(
        status="error",
        message="无法确认凭证状态（HTTP %s）：%s" % (resp.status_code, body_snippet(resp)),
    )


def validate(data) -> list[str]:
    """非空校验，返回错误列表（空列表=通过）；不保证凭据有效。"""
    if not data:
        return ["配置数据为空"]
    if not str(data.get("token") or "").strip():
        return ["API Token不能为空"]
    return []


def get_display_summary(data: dict) -> str:
    return "DigitalOcean"
