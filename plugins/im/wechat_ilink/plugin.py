"""微信 iLink Bot 凭据插件（plugins/im/ 类别下的独立子插件）。

**不做任何联网校验**，状态固定为 error 并提示「仅保存」。

原因：微信 iLink Bot 没有公开可用的凭证自检端点——官方未提供类似
Telegram getMe、飞书 tenant_access_token 的「用凭据换 token」接口，
第三方也无法在不实际发送消息的前提下确认 account + credential 有效。
为避免对真实第三方服务发起请求，这里只做保存。

manifest.json 中 batch_verify=false，前端将其排除在批量验证之外。
"""
from im_common import VerifyResult, guarded

SERVICE = "wechat_ilink"
NAME = "微信 iLink Bot"
REQUIRED = (("account", "账号"), ("credential", "凭据"))
DISPLAY_FIELD = "account"


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return VerifyResult(
        status="error",
        message="该类型暂无公开联网校验接口，仅保存",
    )


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
