"""企业微信凭据验证插件（plugins/im/ 类别下的独立子插件）。

一个插件装两类**企业微信凭据**，由 `kind` 选择器区分（缺省回落 ``app``）：

- ``app``（自建应用）：``GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=&corpsecret=``
  判定：
  - ``errcode == 0`` → valid。换来的 ``access_token`` 属敏感临时值，**绝不存入 account_info**，
    只放 ``expires_in`` 供参考
  - 40001（不合法的 secret）/ 40013（不合法的 CorpID）→ invalid
  - 60020（访问 IP 不在「企业可信 IP」白名单）→ error（配置问题，不是凭据失效）
  - 其它 errcode / HTTP 非 200 → error（拿不准不猜）

- ``webhook``（群机器人）：机器人**只有 POST 一个端点**、没有只读接口，于是用社区通行的
  「非法消息类型」探测法：发一条 ``msgtype`` 为虚构值的请求，企微会在**校验类型**这一步就拒绝、
  **不会把任何内容投递到群里**，回包错误码恰好能区分三种状态：
  - 40008 invalid message type  → 密钥成立（机器人已被认领）⇒ valid
  - 93000 invalid webhook url   → 密钥不成立                      ⇒ invalid
  - 45009 api freq out of limit → 频率限制，与密钥无关            ⇒ error
  最后兜一层：万一机器人真把探测类型当普通消息收下了（errcode 0），如实说明
  「探测内容已被投递」，不假装什么都没发生。

**密钥卫生**：应用态的 corpsecret 与 webhook 态的 key 都走查询串，故任何提示文本、异常、日志
都不得带上完整 URL——网络异常一律只露类型名（``type(exc).__name__``）。

参考文档（gettoken）：https://developer.work.weixin.qq.com/document/path/91039
参考文档（全局错误码）：https://developer.work.weixin.qq.com/document/path/90313
参考文档（群机器人）：https://developer.work.weixin.qq.com/document/path/91770
"""
from __future__ import annotations

from typing import Any

import httpx

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, log, make_client, safe_json

NAME = "企业微信"

KIND_APP = "app"
KIND_WEBHOOK = "webhook"

CORPID_KEY = "corpid"
SECRET_KEY = "corpsecret"
WEBHOOK_KEY = "webhook_key"

GETTOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

# app 形态：gettoken 这一步明确指向 corpid/secret 错误的错误码
_INVALID_CODES = {40001, 40013}   # 40001 不合法的 secret；40013 不合法的 CorpID
_IP_WHITELIST_CODE = 60020        # 不安全的访问 IP（不在白名单）→ 配置问题，非凭据失效

# webhook 形态：以虚构消息类型探测后的错误码
_WEBHOOK_VALID = {40008}      # invalid message type（类型非法 ⇒ 密钥被认领）
_WEBHOOK_INVALID = {93000}    # invalid webhook url（密钥不成立）
_WEBHOOK_BUSY = {45009}       # api freq out of limit（频率限制，与密钥无关）

# 虚构的消息类型：企微在校验类型这一步就拒绝，不会投递任何内容
PROBE_MSGTYPE = "__vault_probe__"


def _kind(data) -> str:
    """凭据形态：`kind` 缺省或非法值一律回落 ``app``（旧凭据没有该字段，仍按应用态校验）。"""
    k = str((data or {}).get("kind") or "").strip()
    return k if k in (KIND_APP, KIND_WEBHOOK) else KIND_APP


def _short(text: Any, limit: int = 160) -> str:
    """把回包文案压到一行；顺手切掉企微 errmsg 里的 ``hint: [...]`` 噪音。"""
    flat = " ".join(str(text or "").split())
    flat = flat.split(", hint:", 1)[0]
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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
    """校验配置，返回错误列表（空列表表示通过）。按 kind 分支，避免 webhook 态却报 CorpID 为空。"""
    data = data or {}
    if not data:
        return ["配置数据为空"]
    if _kind(data) == KIND_WEBHOOK:
        return [] if str(data.get(WEBHOOK_KEY) or "").strip() else ["Webhook 凭据不能为空"]
    errors: list[str] = []
    if not str(data.get(CORPID_KEY) or "").strip():
        errors.append("CorpID 不能为空")
    if not str(data.get(SECRET_KEY) or "").strip():
        errors.append("CorpSecret 不能为空")
    return errors


def _verify_app(corpid: str, corpsecret: str) -> VerifyResult:
    """应用态：GET /cgi-bin/gettoken。

    corpsecret 走 query，只经 ``params`` 传给 httpx，不拼字符串、不进日志/消息。
    """
    with make_client() as client:
        resp = client.get(GETTOKEN_URL, params={"corpid": corpid, "corpsecret": corpsecret})
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


def _verify_webhook(key: str) -> VerifyResult:
    """群机器人态：POST /cgi-bin/webhook/send，发一条虚构消息类型做只读探测。

    密钥只经 ``params`` 传给 httpx，绝不拼进字符串、也不写日志。
    """
    try:
        log(f"校验企业微信群机器人 webhook（密钥长度 {len(key)}）")
        with make_client() as client:
            resp = client.post(
                WEBHOOK_URL,
                params={"key": key},
                headers={"Content-Type": "application/json"},
                json={"msgtype": PROBE_MSGTYPE},
            )
    except httpx.HTTPError as exc:
        # 密钥在查询串里，异常文本可能带 URL ⇒ 只露类型名
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")

    payload = safe_json(resp)
    if not isinstance(payload, dict):
        payload = {}
    code = _errcode(payload)
    detail = _short(payload.get("errmsg") or body_snippet(resp))

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
        return VerifyResult(
            status="valid",
            message=f"密钥有效（{code}：机器人已认领该密钥）",
        )
    if code in _WEBHOOK_BUSY:
        return VerifyResult(status="error", message=f"返回频率限制（{code}），请稍后重试")
    return VerifyResult(
        status="error",
        message=f"无法确认密钥状态（{code}{'，' + detail if detail else ''}）",
    )


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        key = str(data.get(WEBHOOK_KEY) or "").strip()
        if not key:
            return VerifyResult(status="invalid", message="缺少必填字段: Webhook 凭据")
        return _verify_webhook(key)

    corpid = str(data.get(CORPID_KEY) or "").strip()
    corpsecret = str(data.get(SECRET_KEY) or "").strip()
    if not corpid or not corpsecret:
        return VerifyResult(status="invalid", message="缺少必填字段: CorpID / CorpSecret")
    return _verify_app(corpid, corpsecret)


def get_display_summary(data) -> str:
    """列表展示：应用态显示 CorpID（非敏感），webhook 态只显示形态、不回显密钥。"""
    data = data or {}
    if _kind(data) == KIND_WEBHOOK:
        return f"{NAME} | 群机器人"
    corpid = str(data.get(CORPID_KEY) or "").strip()
    return f"{NAME} | {corpid}" if corpid else NAME
