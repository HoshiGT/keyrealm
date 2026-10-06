"""钉钉凭据验证插件（plugins/im/ 类别下的独立子插件）。

一个插件装两类**钉钉凭据**，由 `kind` 选择器区分（缺省回落 ``app``）：

- ``app``（企业内部应用）：两条端点、按顺序尝试
  1. 新版 ``POST https://api.dingtalk.com/v1.0/oauth2/accessToken``（appKey/appSecret 走 JSON body）；
  2. 新版不可用（404/405、非 JSON、5xx、网络异常）时回退旧版
     ``GET https://oapi.dingtalk.com/gettoken?appkey=..&appsecret=..``
     注意：旧版 appsecret 走查询串，属钉钉旧版设计，该 URL 不可透出到消息里。

- ``webhook``（自定义群机器人）：机器人**只有 POST 一个端点**、没有只读接口，于是用社区通行的
  「非法消息类型」探测法：发一条 ``msgType`` 为虚构值的请求，钉钉会在**校验类型**这一步就拒绝、
  **不会把任何内容投递到群里**，回包错误码恰好能区分三种状态：
  - 400105（不支持的类型）/ 310000（sign not match，只是该机器人开了加签）→ token 成立 ⇒ valid
  - 300001 / 300005（token is not exist）→ token 不成立                        ⇒ invalid
  - 660026（sending too many messages per minute）→ 风控限流，与 token 无关
    （实测对无效 token 也会返回）⇒ 「无法确认」，不是无效
  最后兜一层：万一机器人真把探测类型当普通消息收下了（errcode 0），如实说明
  「探测内容已被投递」，不假装什么都没发生。

**密钥卫生**：旧版 appsecret 与 webhook 态的 access_token 都走查询串，故任何进入 message 的
响应文本都不得回显密钥、网络异常只露类型名（``type(exc).__name__``）。

参考文档（企业内部应用 token）：https://open.dingtalk.com/document/orgapp/obtain-orgapp-token
参考文档（自定义机器人）：https://open.dingtalk.com/document/robots/custom-robot-access
"""
from __future__ import annotations

from typing import Any

import httpx

from im_common import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    safe_json,
)

NAME = "钉钉"

KIND_APP = "app"
KIND_WEBHOOK = "webhook"

APP_KEY = "app_key"
APP_SECRET = "app_secret"
WEBHOOK_KEY = "webhook_key"

OAUTH2_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
GETTOKEN_URL = "https://oapi.dingtalk.com/gettoken"
WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"

REQUIRED = (("app_key", "App Key"), ("app_secret", "App Secret"))

# webhook 形态：以虚构消息类型探测后的错误码
_WEBHOOK_VALID = {400105, 310000}     # 类型非法 / 缺签名 —— 都说明 token 存在
_WEBHOOK_INVALID = {300001, 300005}   # token is not exist
_WEBHOOK_THROTTLED = {660026}         # 风控限流，与 token 无关

# 虚构的消息类型：钉钉在校验类型这一步就拒绝，不会投递任何内容
PROBE_MSGTYPE = "__vault_probe__"


def _kind(data) -> str:
    """凭据形态：`kind` 缺省或非法值一律回落 ``app``（旧凭据没有该字段，仍按应用态校验）。"""
    k = str((data or {}).get("kind") or "").strip()
    return k if k in (KIND_APP, KIND_WEBHOOK) else KIND_APP


def _short(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


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


def _errcode(payload) -> int | None:
    raw = (payload or {}).get("errcode")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。按 kind 分支，避免 webhook 态却报 App Key 为空。"""
    data = data or {}
    if not data:
        return ["配置数据为空"]
    if _kind(data) == KIND_WEBHOOK:
        return [] if str(data.get(WEBHOOK_KEY) or "").strip() else ["Webhook 凭据不能为空"]
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
                OAUTH2_URL,
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
                GETTOKEN_URL,
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


def _verify_app(data: dict) -> VerifyResult:
    app_key = (data.get(APP_KEY) or "").strip()
    app_secret = (data.get(APP_SECRET) or "").strip()

    result = _oauth2(app_key, app_secret)
    if result is not None:
        return result
    # 新版端点不可用（404/405、非 JSON、5xx 等）→ 回退旧版
    return _gettoken(app_key, app_secret)


def _verify_webhook(token: str) -> VerifyResult:
    """群机器人态：POST /robot/send，发一条虚构消息类型做只读探测。

    token 只经 ``params`` 传给 httpx，绝不拼进字符串、也不写日志。
    """
    try:
        log(f"校验钉钉群机器人 webhook（token 长度 {len(token)}）")
        with make_client() as client:
            resp = client.post(
                WEBHOOK_URL,
                params={"access_token": token},
                headers={"Content-Type": "application/json"},
                json={"msgtype": PROBE_MSGTYPE},
            )
    except httpx.HTTPError as exc:
        # token 在查询串里，异常文本可能带 URL ⇒ 只露类型名
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")

    payload = safe_json(resp)
    if not isinstance(payload, dict):
        payload = {}
    code = _errcode(payload)
    detail = _short(payload.get("errmsg") or payload.get("message") or body_snippet(resp))

    if code == 0:
        # 探测内容真的被投递了 —— 如实说明，不掩盖
        return VerifyResult(
            status="valid",
            message="密钥有效（注意：该机器人接受了探测类型，探测消息已被投递到群里）",
        )
    if code is None:
        if resp.status_code in (401, 403):
            return VerifyResult(status="invalid", message=f"密钥无效 (HTTP {resp.status_code})")
        return VerifyResult(
            status="error",
            message=f"无法确认密钥状态 (HTTP {resp.status_code})" + (f"：{detail}" if detail else ""),
        )

    if code in _WEBHOOK_INVALID:
        return VerifyResult(
            status="invalid",
            message=f"密钥无效（{code}{'，' + detail if detail else ''}）",
        )
    if code in _WEBHOOK_VALID:
        extra = "，该机器人开启了加签" if code == 310000 else ""
        return VerifyResult(
            status="valid",
            message=f"密钥有效（{code}：机器人已认领该密钥{extra}）",
        )
    if code in _WEBHOOK_THROTTLED:
        return VerifyResult(
            status="error",
            message=f"返回风控限流（{code}），与密钥本身无关，请稍后重试",
        )
    return VerifyResult(
        status="error",
        message=f"无法确认密钥状态（{code}{'，' + detail if detail else ''}）",
    )


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        token = str(data.get(WEBHOOK_KEY) or "").strip()
        if not token:
            return VerifyResult(status="invalid", message="缺少必填字段: Webhook 凭据")
        return _verify_webhook(token)

    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify_app(data)


def get_display_summary(data) -> str:
    """列表展示：应用态显示 App Key（非敏感），webhook 态只显示形态、不回显密钥。"""
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        return f"{NAME} | 群机器人"
    detail = str(data.get(APP_KEY) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
