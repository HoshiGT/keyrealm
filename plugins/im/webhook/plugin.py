"""群机器人 Webhook 验证插件（plugins/im/ 类别下的独立子插件）。

企业微信与钉钉的群机器人**只有 POST 一个端点**，没有只读接口，于是用社区通行的
「非法消息类型」探测法：发一条 ``msgtype`` 为虚构值的请求，两家都会在**校验类型**这一步
就拒绝，**不会把任何内容投递到群里**，而回包里的错误码恰好能区分三种状态：

  企业微信 ``/cgi-bin/webhook/send?key=``
    40008 invalid message type  → 密钥成立（机器人已被认领）⇒ 有效
    93000 invalid webhook url   → 密钥不成立                      ⇒ 无效
  钉钉   ``/robot/send?access_token=``
    400105 不支持类型 msgType    → token 成立                     ⇒ 有效
    310000 sign not match       → token 成立，只是该机器人开了加签 ⇒ 有效
    300001 / 300005 token is not exist → token 不成立             ⇒ 无效
    660026 sending too many messages per minute → 触发了钉钉的风控限流，
        与 token 无关（实测对无效 token 也会返回），⇒ 「无法确认」，不是无效

最后兜一层：万一某台机器人真把探测类型当普通消息收下了（errcode 0），要如实说明
「探测内容已被投递」，不能假装什么都没发生。

密钥走查询串，故任何提示文本、异常、日志都不得带上完整 URL——异常一律只露类型名。
仅从 im_common 复用通用工具。

参考文档（企业微信机器人）：https://developer.work.weixin.qq.com/document/path/91770
参考文档（钉钉机器人）：https://open.dingtalk.com/document/robots/custom-robot-access
"""
from __future__ import annotations

import httpx

from im_common import VerifyResult, body_snippet, guarded, log, make_client, safe_json

NAME = "群机器人 Webhook"
DISPLAY_FIELD = "webhook_key"

KIND_WECOM = "wecom"
KIND_DINGTALK = "dingtalk"
KIND_LABELS = {KIND_WECOM: "企业微信", KIND_DINGTALK: "钉钉"}

WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
DINGTALK_URL = "https://oapi.dingtalk.com/robot/send"

# 虚构的消息类型：两家都会在类型校验阶段拒绝，不会投递任何内容
PROBE_MSGTYPE = "__vault_probe__"

WECOM_VALID = {40008}          # invalid message type
WECOM_INVALID = {93000}        # invalid webhook url
WECOM_BUSY = {45009}           # api freq out of limit

DINGTALK_VALID = {400105, 310000}   # 类型非法 / 缺签名 —— 都说明 token 存在
DINGTALK_INVALID = {300001, 300005}  # token is not exist
DINGTALK_THROTTLED = {660026}        # 风控限流，与 token 无关

_DETAIL_LIMIT = 200


def _short(text: str, limit: int = _DETAIL_LIMIT) -> str:
    """把回包文案压到一行；顺手切掉企微 errmsg 里的 ``hint: [...]`` 噪音。"""
    flat = " ".join(str(text or "").split())
    flat = flat.split(", hint:", 1)[0]
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _post(url: str, key_param: str, key: str, kind: str) -> tuple[int, dict, str]:
    """发探测请求，返回 ``(HTTP 状态码, JSON 载荷, 正文摘要)``。

    密钥只经 ``params`` 传给 httpx，绝不拼进字符串、也不写日志。
    """
    log(f"校验{KIND_LABELS[kind]}群机器人 webhook（密钥长度 {len(key)}）")
    with make_client() as client:
        resp = client.post(
            url,
            params={key_param: key},
            headers={"Content-Type": "application/json"},
            json={"msgtype": PROBE_MSGTYPE},
        )
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        payload = {}
    return resp.status_code, payload, body_snippet(resp)


def _code(payload: dict):
    """钉钉/企微都用 ``errcode``，统一成 int（拿不到就 None）。"""
    raw = payload.get("errcode")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _finish(status: int, payload: dict, snippet: str, kind: str) -> VerifyResult:
    label = KIND_LABELS[kind]
    code = _code(payload)
    detail = _short(payload.get("errmsg") or payload.get("message") or snippet)

    if code is None:
        if status in (401, 403):
            return VerifyResult(status="invalid", message=f"{label}密钥无效 (HTTP {status})")
        return VerifyResult(
            status="error",
            message=f"无法确认{label}密钥状态 (HTTP {status})" + (f"：{detail}" if detail else ""),
        )

    if code == 0:
        # 探测内容真的被投递了 —— 如实说明，不掩盖
        return VerifyResult(
            status="valid",
            message=f"{label}密钥有效（注意：该机器人接受了探测类型，探测消息已被投递到群里）",
        )

    valid_codes, invalid_codes = (
        (WECOM_VALID, WECOM_INVALID) if kind == KIND_WECOM else (DINGTALK_VALID, DINGTALK_INVALID)
    )

    if code in invalid_codes:
        return VerifyResult(
            status="invalid",
            message=f"{label}密钥无效（{code}{'，' + detail if detail else ''}）",
        )
    if code in valid_codes:
        extra = "，该机器人开启了加签" if code == 310000 else ""
        return VerifyResult(
            status="valid",
            message=f"{label}密钥有效（{code}：机器人已认领该密钥{extra}）",
        )
    if code in WECOM_BUSY:
        return VerifyResult(status="error", message=f"{label}返回频率限制（{code}），请稍后重试")
    if code in DINGTALK_THROTTLED:
        return VerifyResult(
            status="error",
            message=f"{label}返回风控限流（{code}），与密钥本身无关，请稍后重试",
        )
    return VerifyResult(
        status="error",
        message=f"无法确认{label}密钥状态（{code}{'，' + detail if detail else ''}）",
    )


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    return [] if str(data.get(DISPLAY_FIELD) or "").strip() else ["Webhook 凭据不能为空"]


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    key = str(data.get(DISPLAY_FIELD) or "").strip()
    if not key:
        return VerifyResult(status="invalid", message="缺少必填字段: Webhook 凭据")

    kind = str(data.get("kind") or "").strip()
    if kind not in KIND_LABELS:
        kind = KIND_WECOM

    try:
        if kind == KIND_DINGTALK:
            status, payload, snippet = _post(DINGTALK_URL, "access_token", key, kind)
        else:
            status, payload, snippet = _post(WECOM_URL, "key", key, kind)
    except httpx.HTTPError as exc:
        # 密钥在查询串里，异常文本可能带 URL ⇒ 只露类型名
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")

    return _finish(status, payload, snippet, kind)


def get_display_summary(data) -> str:
    """列表展示只用类型名，不回显密钥。"""
    data = data or {}
    kind = str(data.get("kind") or "").strip()
    return f"{NAME} | {KIND_LABELS.get(kind, KIND_LABELS[KIND_WECOM])}"
