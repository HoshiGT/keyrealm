"""即时通讯类别共享模块：各服务凭证验证处理器。

plugins/im/ 下的每个子插件（feishu、dingtalk、telegram……）都是独立插件，
通过 `from im_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持飞书应用、钉钉、Telegram Bot、Discord Webhook、QQ 频道机器人、
微信 iLink Bot，共 6 类即时通讯凭据。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。

安全约定：
- account_info 与 display_summary 只输出非敏感信息（应用 ID、机器人 ID、
  渠道名等公开标识），绝不回显 app_secret / bot_token / webhook_url /
  client_secret / credential 等密钥明文；
- Telegram Bot 与 Discord Webhook 的密钥位于 URL 中，网络异常时只返回
  异常类型，避免异常文本连带 URL 一起把密钥写进消息。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


# 请求超时（秒）
TIMEOUT = 20.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


# ── 服务配置 ─────────────────────────────────────────────
#
# required_fields: ((字段名, 中文标签), ...)，用于校验与必填判定。
SERVICE_CONFIGS = {
    "feishu": {
        "name": "飞书应用",
        "required_fields": (("app_id", "App ID"), ("app_secret", "App Secret")),
    },
    "dingtalk": {
        "name": "钉钉",
        "required_fields": (("app_key", "App Key"), ("app_secret", "App Secret")),
    },
    "telegram": {
        "name": "Telegram Bot",
        "required_fields": (("bot_token", "Bot Token"),),
    },
    "discord": {
        "name": "Discord Webhook",
        "required_fields": (("webhook_url", "Webhook URL"),),
    },
    "qq_bot": {
        "name": "QQ 频道机器人",
        "required_fields": (("app_id", "App ID"), ("client_secret", "Client Secret")),
    },
    "wechat_ilink": {
        "name": "微信 iLink Bot",
        "required_fields": (("account", "账号"), ("credential", "凭据")),
    },
}


def _log(message: str) -> None:
    print(f"[im] {message}", file=sys.stderr)


# ── 请求封装 ─────────────────────────────────────────────

def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def _client(follow_redirects: bool = True) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=follow_redirects)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _safe_request_error(exc: Exception) -> VerifyResult:
    """网络异常兜底：只暴露异常类型。

    部分服务的密钥直接拼在请求 URL 里（Telegram Bot Token、Discord
    Webhook Token、钉钉旧版 appsecret 查询参数），httpx 异常的字符串
    表示通常包含完整 URL，直接透出会把密钥写进凭据记录。
    """
    return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")


def _error_detail(payload: Optional[dict]) -> str:
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


# ── 各服务验证 ───────────────────────────────────────────

def _verify_feishu(data: dict) -> VerifyResult:
    """飞书自建/商店应用的 tenant_access_token 换取接口。

    成功时返回 tenant_access_token，失败时 HTTP 200 + code != 0，
    常见错误码：10003（app_id 不存在）、10007、99991663（app_id/secret 不匹配）。
    """
    app_id = (data.get("app_id") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    with _client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"app_id": app_id, "app_secret": app_secret},
        )
    status = resp.status_code
    payload = _safe_json(resp)
    if isinstance(payload, dict):
        code = payload.get("code")
        if code in (0, "0"):
            info: dict[str, Any] = {"app_id": app_id}
            if payload.get("expire") is not None:
                info["expires_in"] = payload["expire"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info)
        if code is not None:
            detail = _error_detail(payload)
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_dingtalk(data: dict) -> VerifyResult:
    """钉钉企业内部应用：优先新版 v1.0 端点，失败时回退旧版 gettoken。"""
    app_key = (data.get("app_key") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()

    result = _dingtalk_oauth2(app_key, app_secret)
    if result is not None:
        return result
    # 新版端点不可用（404/405、非 JSON、5xx 等）→ 回退旧版
    return _dingtalk_gettoken(app_key, app_secret)


def _dingtalk_oauth2(app_key: str, app_secret: str) -> Optional[VerifyResult]:
    """POST /v1.0/oauth2/accessToken；端点不可用（需回退）时返回 None。"""
    try:
        with _client() as client:
            resp = client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                headers={"Content-Type": "application/json"},
                json={"appKey": app_key, "appSecret": app_secret},
            )
    except httpx.HTTPError as exc:
        _log(f"钉钉新版端点请求失败，回退旧版: {type(exc).__name__}")
        return None

    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        if isinstance(payload, dict) and payload.get("accessToken"):
            info: dict[str, Any] = {"app_key": app_key}
            if payload.get("expireIn") is not None:
                info["expires_in"] = payload["expireIn"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info)
        detail = _error_detail(payload) or _body_snippet(resp)
        return VerifyResult(status="invalid", message=f"凭证无效: {detail}" if detail else "凭证无效")
    if status in (400, 401, 403):
        detail = _error_detail(payload) or _body_snippet(resp)
        return VerifyResult(
            status="invalid",
            message=f"凭证无效: {detail}" if detail else f"凭证无效 (HTTP {status})",
        )
    if status in (404, 405):
        return None
    _log(f"钉钉新版端点异常响应 HTTP {status}，回退旧版")
    return None


def _dingtalk_gettoken(app_key: str, app_secret: str) -> VerifyResult:
    """旧版 GET /gettoken?appkey=..&appsecret=..（appsecret 走查询串，属钉钉旧版设计）。"""
    try:
        with _client() as client:
            resp = client.get(
                "https://oapi.dingtalk.com/gettoken",
                params={"appkey": app_key, "appsecret": app_secret},
            )
    except httpx.HTTPError as exc:
        return _safe_request_error(exc)

    status = resp.status_code
    payload = _safe_json(resp)
    if isinstance(payload, dict):
        errcode = payload.get("errcode")
        if errcode in (0, "0") and payload.get("access_token"):
            info: dict[str, Any] = {"app_key": app_key}
            if payload.get("expires_in") is not None:
                info["expires_in"] = payload["expires_in"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info)
        if errcode is not None:
            detail = _error_detail(payload)
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_telegram(data: dict) -> VerifyResult:
    """Telegram Bot：GET /bot<token>/getMe，成功返回机器人自身信息。"""
    token = (data.get("bot_token") or "").strip()
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with _client() as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        # token 位于 URL 中，异常文本不可直接透出
        return _safe_request_error(exc)

    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200 and isinstance(payload, dict) and payload.get("ok"):
        result = payload.get("result")
        info: dict[str, Any] = {}
        if isinstance(result, dict):
            for key in ("id", "username", "first_name"):
                if result.get(key) is not None:
                    info[key] = result[key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if isinstance(payload, dict) and payload.get("ok") is False:
        detail = _error_detail(payload) or "Bot Token 无效"
        return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 404):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_discord(data: dict) -> VerifyResult:
    """Discord Webhook：GET Webhook URL 本身即可返回该 Webhook 的元数据。

    关闭重定向：Webhook URL 由用户提供，跟随跳转会把请求带去未知目标。
    """
    url = (data.get("webhook_url") or "").strip()
    try:
        with _client(follow_redirects=False) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        # token 位于 URL 中，异常文本不可直接透出
        return _safe_request_error(exc)

    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200 and isinstance(payload, dict):
        info: dict[str, Any] = {}
        for key in ("name", "channel_id", "guild_id"):
            if payload.get(key) is not None:
                info[key] = payload[key]
        # 注意：响应中的 token 字段与请求 URL 中的 token 同为敏感信息，不写入 account_info
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message="凭证无效（Webhook Token 无效或已被重置）")
    if status == 404:
        return VerifyResult(status="invalid", message="凭证无效（Webhook 不存在或已被删除）")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_qq_bot(data: dict) -> VerifyResult:
    """QQ 频道机器人：POST /app/getAppAccessToken 换取 AppAccessToken。"""
    app_id = (data.get("app_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    url = "https://bots.qq.com/app/getAppAccessToken"
    with _client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"appId": app_id, "clientSecret": client_secret},
        )
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
        info: dict[str, Any] = {"app_id": app_id}
        if payload.get("expires_in") is not None:
            info["expires_in"] = payload["expires_in"]
        return VerifyResult(status="valid", message="凭证有效", account_info=info)
    if isinstance(payload, dict):
        detail = _error_detail(payload)
        if detail:
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_wechat_ilink(data: dict) -> VerifyResult:
    """微信 iLink Bot（形如 xxx@im.bot）：不做任何联网校验。

    原因：微信 iLink Bot 没有公开可用的凭证自检端点——官方未提供类似
    Telegram getMe、飞书 tenant_access_token 的「用凭据换 token」接口，
    第三方也无法在不实际发送消息的前提下确认 account + credential 有效。
    为避免对真实第三方服务发起请求，这里只做保存，状态固定为 error。
    """
    return VerifyResult(
        status="error",
        message="该类型暂无公开联网校验接口，仅保存",
    )


_SERVICE_VERIFIERS: dict[str, Callable[[dict], VerifyResult]] = {
    "feishu": _verify_feishu,
    "dingtalk": _verify_dingtalk,
    "telegram": _verify_telegram,
    "discord": _verify_discord,
    "qq_bot": _verify_qq_bot,
    "wechat_ilink": _verify_wechat_ilink,
}


# ── 字段校验（validate） ─────────────────────────────────

def _validate_telegram(data: dict) -> list[str]:
    token = (data.get("bot_token") or "").strip()
    if token and ":" not in token:
        return ["Bot Token 格式应为 <bot_id>:<secret>"]
    return []


def _validate_discord(data: dict) -> list[str]:
    url = (data.get("webhook_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return ["Webhook URL 必须以 http:// 或 https:// 开头"]
    return []


_EXTRA_VALIDATORS: dict[str, Callable[[dict], list[str]]] = {
    "telegram": _validate_telegram,
    "discord": _validate_discord,
}


# ── 列表展示（display_summary） ──────────────────────────
#
# 只取非敏感标识：应用 ID / 机器人 ID / Webhook ID / 账号名，
# 其中的密钥部分一律不展示。

def _telegram_bot_id(token: str) -> str:
    """Telegram Bot Token 形如 <bot_id>:<secret>，只取 bot_id。"""
    return token.split(":", 1)[0].strip()


def _discord_webhook_id(url: str) -> str:
    """从 Webhook URL 路径 /api/webhooks/<id>/<token> 中只取 <id>。"""
    try:
        path = httpx.URL(url).path
    except Exception:
        return ""
    parts = [seg for seg in path.split("/") if seg]
    for idx, seg in enumerate(parts):
        if seg == "webhooks" and idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def _detail_telegram(data: dict) -> str:
    bot_id = _telegram_bot_id((data.get("bot_token") or "").strip())
    return f"Bot {bot_id}" if bot_id else ""


def _detail_discord(data: dict) -> str:
    webhook_id = _discord_webhook_id((data.get("webhook_url") or "").strip())
    return f"Webhook {webhook_id}" if webhook_id else ""


_DISPLAY_FIELDS = {
    "feishu": "app_id",
    "dingtalk": "app_key",
    "qq_bot": "app_id",
    "wechat_ilink": "account",
}

_DISPLAY_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "telegram": _detail_telegram,
    "discord": _detail_discord,
}


# ── 子插件入口（service 由子插件显式传入） ───────────────

def validate_common(service: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = SERVICE_CONFIGS.get(service)
    if config is None:
        return [f"不支持的服务: {service}"]

    errors: list[str] = []
    for key, label in config["required_fields"]:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")

    extra = _EXTRA_VALIDATORS.get(service)
    if extra is not None:
        errors.extend(extra(data))

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的凭据是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        missing = [
            label
            for key, label in config["required_fields"]
            if not str(data.get(key) or "").strip()
        ]
        if missing:
            return VerifyResult(
                status="invalid",
                message=f"缺少必填字段: {'、'.join(missing)}",
            )

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")
        return verifier(data)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        # 异常文本可能包含带密钥的 URL，这里只保留异常类型
        return _safe_request_error(exc)
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {exc!r}")
        return VerifyResult(status="error", message=f"插件执行异常: {exc}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本（只含非敏感标识）。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)

    formatter = _DISPLAY_FORMATTERS.get(service)
    if formatter is not None:
        detail = formatter(data)
    else:
        field = _DISPLAY_FIELDS.get(service)
        detail = str(data.get(field) or "").strip() if field else ""

    return f"{name} | {detail}" if detail else name
