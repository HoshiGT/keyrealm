"""企业微信应用凭据验证插件（plugins_optin/im/wecom/，opt-in 未用真实凭据实测）。

端点：``GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=<>&corpsecret=<>``
鉴权：corpid + corpsecret 作为 query 参数（企业微信 gettoken 的官方用法）。
判定：
- ``errcode == 0`` → valid。换来的 ``access_token`` 属敏感临时值，**绝不存入
  account_info**，只放 ``expires_in`` 供参考
- ``errcode`` 明确指向 corpid/secret 错误 → invalid：40001（不合法的 secret）、
  40013（不合法的 CorpID）
- ``errcode == 60020``（访问 IP 不在「企业可信 IP」白名单）→ error（配置问题，
  不是凭据失效）
- 其它 errcode / HTTP 非 200 → error（拿不准不猜）

**注意**：corpsecret 在 query 里，任何进入 message 的响应文本都先净化、绝不回显
corpsecret；网络异常只露类型名（异常文本可能带完整 URL）。

已确认官方文档：developer.work.weixin.qq.com/document/path/91039（gettoken 请求
方式、参数与返回字段 errcode/errmsg/access_token/expires_in）与
/document/path/90313（全局错误码对照表，逐条核实 40001 / 40013 / 40014 / 41001 /
42001 / 60020 的含义），2026-10-06 查询。其中 40014 / 41001 / 42001 是
access_token 相关的错误，不出现在 gettoken 这一步，故未纳入 invalid 判据。

参考文档：https://developer.work.weixin.qq.com/document/path/91039
参考文档：https://developer.work.weixin.qq.com/document/path/90313
"""
from __future__ import annotations

from typing import Any

from im_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "企业微信应用"
CORPID_KEY = "corpid"
SECRET_KEY = "corpsecret"
ENDPOINT = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"

# gettoken 这一步明确指向 corpid/secret 错误的错误码
_INVALID_CODES = {40001, 40013}   # 40001 不合法的 secret；40013 不合法的 CorpID
_IP_WHITELIST_CODE = 60020        # 不安全的访问 IP（不在白名单）→ 配置问题，非凭据失效


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _errcode(payload) -> int | None:
    raw = (payload or {}).get("errcode")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not str(data.get(CORPID_KEY) or "").strip():
        errors.append("CorpID 不能为空")
    if not str(data.get(SECRET_KEY) or "").strip():
        errors.append("CorpSecret 不能为空")
    return errors


def _verify_wecom(corpid: str, corpsecret: str) -> VerifyResult:
    """企业微信凭据验证：GET /cgi-bin/gettoken。

    corpsecret 走 query，只经 ``params`` 传给 httpx，不拼字符串、不进日志/消息。
    """
    with make_client() as client:
        resp = client.get(ENDPOINT, params={"corpid": corpid, "corpsecret": corpsecret})
    status = resp.status_code
    payload = safe_json(resp)
    code = _errcode(payload) if isinstance(payload, dict) else None

    if code == 0:
        info: dict[str, Any] = {}
        # access_token 是换来的敏感值，绝不进 account_info；只放有效期
        expires = (payload or {}).get("expires_in")
        if expires is not None:
            try:
                info["expires_in"] = int(expires)
            except (TypeError, ValueError):
                pass
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if code in _INVALID_CODES:
        detail = _short((payload or {}).get("errmsg") or "")
        return VerifyResult(
            status="invalid",
            message=f"凭证无效（{code}" + (f"，{detail}" if detail else "") + "）",
        )

    if code == _IP_WHITELIST_CODE:
        return VerifyResult(
            status="error",
            message="访问 IP 不在企业可信 IP 白名单内（60020），请到企业微信后台配置后重试",
        )

    if code is not None:
        detail = _short((payload or {}).get("errmsg") or "")
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态（{code}" + (f"，{detail}" if detail else "") + "）",
        )

    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    corpid = str(data.get(CORPID_KEY) or "").strip()
    corpsecret = str(data.get(SECRET_KEY) or "").strip()
    if not corpid or not corpsecret:
        return VerifyResult(status="invalid", message="缺少必填字段: CorpID / CorpSecret")
    return _verify_wecom(corpid, corpsecret)


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本：只显示 CorpID（非敏感），绝不含 CorpSecret。"""
    data = data or {}
    corpid = str(data.get(CORPID_KEY) or "").strip()
    return f"{NAME} | {corpid}" if corpid else NAME
