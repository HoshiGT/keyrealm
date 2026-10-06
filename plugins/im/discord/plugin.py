"""Discord Webhook 验证插件（plugins/im/ 类别下的独立子插件）。

端点：GET Webhook URL 本身即可返回该 Webhook 的元数据。

关闭重定向：Webhook URL 由用户提供，跟随跳转会把请求带去未知目标。
安全提示：Webhook Token 位于 URL 路径中，网络异常只返回异常类型；响应里的
token 字段也不写入 account_info。

参考文档：https://docs.discord.com/developers/resources/webhook
"""
import httpx

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE = "discord"
NAME = "Discord Webhook"
REQUIRED = (("webhook_url", "Webhook URL"),)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _safe_request_error(exc: Exception) -> VerifyResult:
    """网络异常兜底：只暴露异常类型（token 在 URL 中）。"""
    return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    url = (data.get("webhook_url") or "").strip()
    if not url:
        errors.append("Webhook URL 不能为空")
    elif not url.lower().startswith(("http://", "https://")):
        errors.append("Webhook URL 必须以 http:// 或 https:// 开头")
    return errors


def _verify(data: dict) -> VerifyResult:
    url = (data.get("webhook_url") or "").strip()
    try:
        with make_client(follow_redirects=False) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        # token 位于 URL 中，异常文本不可直接透出
        return _safe_request_error(exc)

    status = resp.status_code
    payload = safe_json(resp)
    if status == 200 and isinstance(payload, dict):
        info: dict = {}
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
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify(data)


def _webhook_id(url: str) -> str:
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


# ── 可选操作：查看 Webhook 信息（只读）────────────────────

def _as_text(value) -> str:
    """统一转字符串并去除首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def actions(data) -> list[dict]:
    """声明可用操作（凭据含 webhook_url 时提供）。"""
    data = data or {}
    if not _as_text(data.get("webhook_url")):
        return []
    return [{
        "id": "webhook_info",
        "name": "查看 Webhook 信息",
        "kind": "form",
        "submit": "查询",
        "fields": [],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：读取 Webhook 元数据（只读）。

    关闭重定向（与 verify 一致）；错误只暴露异常类型与状态码，绝不带 URL
    （token 位于 URL 路径中）；响应里的 token 字段不回显。
    """
    data = data or {}
    if action_id != "webhook_info":
        return {"ok": False, "error": "未知操作"}
    url = _as_text(data.get("webhook_url"))
    if not url:
        return {"ok": False, "error": "缺少 Webhook URL"}
    try:
        try:
            with make_client(follow_redirects=False) as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"网络请求失败：{type(exc).__name__}"}
        payload = safe_json(resp)
        if resp.status_code != 200 or not isinstance(payload, dict):
            return {"ok": False, "error": f"查询失败 (HTTP {resp.status_code})"}
        # 注意：不输出响应中的 token 字段（与凭据同源，属敏感信息）
        lines = [
            f"名称：{payload.get('name') or '-'}",
            f"频道 ID：{payload.get('channel_id') or '-'}",
            f"服务器 ID：{payload.get('guild_id') or '-'}",
        ]
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    data = data or {}
    webhook_id = _webhook_id((data.get("webhook_url") or "").strip())
    detail = f"Webhook {webhook_id}" if webhook_id else ""
    return f"{NAME} | {detail}" if detail else NAME
