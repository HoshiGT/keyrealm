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

# 本插件当前**不发任何网络请求**（只做本地格式校验），代理选项没有意义，故显式排除。
# 注意：iLink Bot 其实有可用的只读校验端点（POST /ilink/bot/getupdates，timeout=0 立即返回）；
# 若日后接入该探针，此处要改回 `USES_PROXY = True`（那是 HTTP 请求，走 make_client）。
USES_PROXY = False


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
