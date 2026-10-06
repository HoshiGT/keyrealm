"""Telegram Bot Token 验证插件（plugins/im/ 类别下的独立子插件）。

端点：GET https://api.telegram.org/bot<token>/getMe，成功返回机器人自身信息。

安全提示：Bot Token 直接拼在 URL 路径中，任何异常文本都可能带完整 URL，
因此网络异常只返回异常类型（见 im_common.guarded）。

参考文档：https://core.telegram.org/bots/api
"""
import httpx

from im_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

SERVICE = "telegram"
NAME = "Telegram Bot"
REQUIRED = (("bot_token", "Bot Token"),)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _safe_request_error(exc: Exception) -> VerifyResult:
    """网络异常兜底：只暴露异常类型（token 在 URL 中）。"""
    return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")


def _error_detail(payload) -> str:
    """尽力从错误响应中提取可读原因。"""
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


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    token = (data.get("bot_token") or "").strip()
    if not token:
        errors.append("Bot Token 不能为空")
    elif ":" not in token:
        errors.append("Bot Token 格式应为 <bot_id>:<secret>")
    return errors


def _verify(data: dict) -> VerifyResult:
    token = (data.get("bot_token") or "").strip()
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with make_client() as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        # token 位于 URL 中，异常文本不可直接透出
        return _safe_request_error(exc)

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict) and payload.get("ok"):
        result = payload.get("result")
        info: dict = {}
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
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify(data)


def _bot_id(token: str) -> str:
    """Telegram Bot Token 形如 <bot_id>:<secret>，只取 bot_id。"""
    return token.split(":", 1)[0].strip()


# ── 可选操作：查看机器人信息（只读）/ 发送消息（写操作）────────────

def _as_text(value) -> str:
    """统一转字符串并去除首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def actions(data) -> list[dict]:
    """声明可用操作（凭据含 bot_token 时提供）。"""
    data = data or {}
    if not _as_text(data.get("bot_token")):
        return []
    return [
        {
            "id": "bot_info",
            "name": "查看机器人信息",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
        {
            "id": "send_message",
            "name": "发送消息（写操作）",
            "kind": "form",
            "submit": "发送",
            "write": True,
            "fields": [
                {
                    "key": "chat_id",
                    "label": "会话 ID",
                    "type": "text",
                    "default": "",
                    "placeholder": "@频道名 或 数字 ID",
                    "required": True,
                },
                {
                    "key": "text",
                    "label": "消息内容",
                    "type": "textarea",
                    "default": "",
                    "placeholder": "支持 Markdown",
                    "required": True,
                },
            ],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看机器人信息（只读）/ 发送消息（写操作）。

    错误消息只暴露异常类型或 Telegram 自身 description，绝不带 URL
    （token 位于 URL 路径中）。
    """
    data = data or {}
    params = params or {}
    token = _as_text(data.get("bot_token"))
    if not token:
        return {"ok": False, "error": "缺少 Bot Token"}
    # token 位于 URL 路径，异常/非 JSON 响应可能回带 URL，故仅取异常类型与状态码
    base = f"https://api.telegram.org/bot{token}"
    try:
        if action_id == "bot_info":
            try:
                with make_client() as client:
                    resp = client.get(f"{base}/getMe")
            except httpx.HTTPError as exc:
                return {"ok": False, "error": f"网络请求失败：{type(exc).__name__}"}
            payload = safe_json(resp)
            if not isinstance(payload, dict):
                return {"ok": False, "error": f"响应异常 (HTTP {resp.status_code})"}
            if not payload.get("ok"):
                return {"ok": False, "error": f"查询失败：{_error_detail(payload) or '未知错误'}"}
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            lines = [
                f"机器人 ID：{result.get('id', '-')}",
                f"用户名：{result.get('username', '-')}",
                f"名称：{result.get('first_name', '-')}",
            ]
            return {"ok": True, "output": "\n".join(lines)}

        if action_id == "send_message":
            chat_id = _as_text(params.get("chat_id"))
            text = "" if params.get("text") is None else str(params.get("text"))
            if not chat_id:
                return {"ok": False, "error": "请填写会话 ID"}
            if not text.strip():
                return {"ok": False, "error": "消息内容不能为空"}
            try:
                with make_client() as client:
                    resp = client.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text})
            except httpx.HTTPError as exc:
                return {"ok": False, "error": f"网络请求失败：{type(exc).__name__}"}
            payload = safe_json(resp)
            if isinstance(payload, dict) and payload.get("ok"):
                return {"ok": True, "output": "已发送"}
            detail = _error_detail(payload) if isinstance(payload, dict) else ""
            return {"ok": False, "error": f"发送失败：{detail or f'HTTP {resp.status_code}'}"}

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    data = data or {}
    bot_id = _bot_id((data.get("bot_token") or "").strip())
    detail = f"Bot {bot_id}" if bot_id else ""
    return f"{NAME} | {detail}" if detail else NAME
