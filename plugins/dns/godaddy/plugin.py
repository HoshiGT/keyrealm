"""GoDaddy API Key 验证插件（plugins/dns/ 类别下的独立子插件）。"""
from dns_common import VerifyResult, body_snippet, guarded, make_client


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_godaddy(data: dict) -> VerifyResult:
    """GoDaddy：调用域名列表接口，检查 HTTP 状态码与返回列表。"""
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()

    with make_client() as client:
        resp = client.get(
            "https://api.godaddy.com/v1/domains",
            params={"limit": 1},
            headers={
                "Authorization": f"sso-key {api_key}:{api_secret}",
                "Accept": "application/json",
            },
        )

    status = resp.status_code
    if status == 200:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        account_info = {"domain_count": len(payload)} if isinstance(payload, list) else None
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")

    return _unconfirmed(status, body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("GoDaddy API Key 不能为空")
    if not (data.get("api_secret") or "").strip():
        errors.append("GoDaddy API Secret 不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_godaddy(data)


# ── 可选操作：查看域名 / 解析记录（只读）────────────────

_GODADDY_API = "https://api.godaddy.com/v1"


def _auth_headers(data: dict) -> dict:
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()
    return {
        "Authorization": f"sso-key {api_key}:{api_secret}",
        "Accept": "application/json",
    }


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 api_key / api_secret 时提供域名查询）。"""
    data = data or {}
    if not ((data.get("api_key") or "").strip() and (data.get("api_secret") or "").strip()):
        return []
    return [
        {
            "id": "list_records",
            "name": "查看域名解析记录",
            "kind": "form",
            "submit": "查询",
            "fields": [
                {"key": "domain", "label": "域名", "type": "text", "placeholder": "example.com"},
            ],
        },
        {
            "id": "list_domains",
            "name": "查看域名列表",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
    ]


def _godaddy_get(path: str, data: dict, **params) -> tuple[int, object, str]:
    """GET 请求，返回 (HTTP 状态码, 解析后的 JSON 或 None, 正文摘要)。

    本接口的密钥放在请求头而非 URL，错误信息中仍不拼完整 URL，避免意外外泄。
    """
    with make_client() as client:
        resp = client.get(f"{_GODADDY_API}{path}", params=params, headers=_auth_headers(data))
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON（网关错误页等）
        payload = None
    return resp.status_code, payload, body_snippet(resp)


def _error_message(status: int, payload: object, snippet: str) -> str:
    """由 GoDaddy 错误体提取 message/code；不含 URL 与密钥。"""
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("code") or "").strip()
    if not detail:
        detail = snippet
    return f"查询失败（HTTP {status}）" + (f"：{detail}" if detail else "")


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看 GoDaddy 域名列表 / 指定域名的解析记录（只读）。"""
    data = data or {}
    params = params or {}
    try:
        if action_id == "list_records":
            domain = (params.get("domain") or "").strip()
            if not domain:
                return {"ok": False, "error": "请填写域名"}
            status, payload, snippet = _godaddy_get(f"/domains/{domain}/records", data)
            if status != 200:
                return {"ok": False, "error": _error_message(status, payload, snippet)}
            records = payload if isinstance(payload, list) else []
            lines = [f"{domain} 共 {len(records)} 条解析：", ""]
            for record in records:
                if not isinstance(record, dict):
                    continue
                lines.append(
                    f"{str(record.get('type', '')):<6} {str(record.get('name', '')):<12} "
                    f"{record.get('data', '')}  TTL={record.get('ttl', '')}"
                )
            if not records:
                lines.append("（无解析记录）")
            return {"ok": True, "output": "\n".join(lines)}

        if action_id == "list_domains":
            status, payload, snippet = _godaddy_get("/domains", data, limit=50)
            if status != 200:
                return {"ok": False, "error": _error_message(status, payload, snippet)}
            domains = payload if isinstance(payload, list) else []
            lines = [f"共 {len(domains)} 个域名：", ""]
            for item in domains:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"{item.get('domain', '')}  状态={item.get('status', '')}  "
                    f"到期={str(item.get('expires', ''))[:10]}"
                )
            if not domains:
                lines.append("（无域名）")
            return {"ok": True, "output": "\n".join(lines)}

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return "GoDaddy"
    masked = f"{api_key[:4]}..." if len(api_key) > 4 else "已配置"
    return f"GoDaddy | API Key: {masked}"
