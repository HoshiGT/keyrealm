"""GoDaddy API Key 验证插件（plugins/dns/ 类别下的独立子插件）。

操作（凭据含 api_key / api_secret 时提供）：
- ``list_domains``（列表）域名列表，行内可下钻 ``list_records``（解析记录）
  或打开 ``add_record``（新增解析）并自动带域名；
- ``list_records``（列表）指定域名的解析记录，行内 ``edit_record``（修改解析）；
- ``add_record``（表单，写操作）PATCH 追加一条解析记录；
  ``edit_record``（表单，写操作）PUT 覆盖该「类型 + 主机名」下的全部记录。

刻意不提供删除解析：破坏性且不属于本插件的验证目标。
"""
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


# ── 可选操作：域名 / 解析记录管理 ───────────────────────

_GODADDY_API = "https://api.godaddy.com/v1"

# 记录类型与默认 TTL（GoDaddy 记录里 name 是主机记录、data 是记录值）
_RECORD_TYPES = ["A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA"]


def _auth_headers(data: dict) -> dict:
    api_key = (data.get("api_key") or "").strip()
    api_secret = (data.get("api_secret") or "").strip()
    return {
        "Authorization": f"sso-key {api_key}:{api_secret}",
        "Accept": "application/json",
    }


def _record_fields() -> list[dict]:
    """新增 / 修改解析记录的表单字段。

    GoDaddy 的修改**按「类型 + 主机名」整体覆盖**，没有记录 ID 可用，
    因此修改表单与新增同形，只靠提交按钮文案与 description 区分语义。
    """
    return [
        {"key": "domain", "label": "域名", "type": "text", "required": True, "placeholder": "example.com"},
        {"key": "record_type", "label": "记录类型", "type": "select", "options": _RECORD_TYPES, "default": "A"},
        {"key": "name", "label": "主机记录", "type": "text", "default": "@", "placeholder": "@ / www"},
        {"key": "data", "label": "记录值", "type": "text", "required": True, "placeholder": "如 1.2.3.4"},
        {"key": "ttl", "label": "TTL", "type": "text", "default": "600", "placeholder": "秒，最小 600"},
    ]


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 api_key / api_secret 时提供域名与解析管理）。"""
    data = data or {}
    if not ((data.get("api_key") or "").strip() and (data.get("api_secret") or "").strip()):
        return []
    return [
        {
            "id": "list_domains",
            "name": "域名列表",
            "kind": "list",
            "columns": [
                {"key": "name", "label": "域名", "mono": True},
                {"key": "status", "label": "状态", "width": 110},
                {"key": "expires", "label": "到期", "width": 120},
            ],
            "row_actions": [
                {"id": "list_records", "label": "解析", "params": {"domain": "$row.name"}},
                {"id": "add_record", "label": "新增", "params": {"domain": "$row.name"}},
            ],
            "empty_text": "该账号下没有域名",
        },
        {
            "id": "list_records",
            "name": "解析记录",
            "kind": "list",
            "columns": [
                {"key": "name", "label": "主机记录", "mono": True},
                {"key": "type", "label": "记录类型", "type": "tag", "width": 100},
                {"key": "data", "label": "记录值", "mono": True},
                {"key": "ttl", "label": "TTL", "width": 90},
            ],
            # 行内「修改」把该记录预填进 edit_record 表单（domain 不在列里，仅供模板取值）
            "row_actions": [
                {
                    "id": "edit_record",
                    "label": "修改",
                    "params": {
                        "domain": "$row.domain",
                        "record_type": "$row.type",
                        "name": "$row.name",
                        "data": "$row.data",
                        "ttl": "$row.ttl",
                    },
                },
            ],
            "empty_text": "该域名下没有解析记录",
        },
        {
            "id": "add_record",
            "name": "新增解析",
            "kind": "form",
            "submit": "新增",
            "write": True,
            "fields": _record_fields(),
        },
        {
            "id": "edit_record",
            "name": "修改解析",
            "kind": "form",
            "submit": "覆盖保存",
            "write": True,
            "fields": _record_fields(),
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


def _godaddy_write(method: str, path: str, data: dict, body: object) -> tuple[int, object, str]:
    """PATCH / PUT 请求（写操作），返回与 ``_godaddy_get`` 相同的三元组。

    密钥同样只在请求头；``json=`` 由 httpx 自动带上 Content-Type。
    """
    with make_client() as client:
        resp = client.request(
            method, f"{_GODADDY_API}{path}", json=body, headers=_auth_headers(data)
        )
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON（网关错误页等）
        payload = None
    return resp.status_code, payload, body_snippet(resp)


def _error_message(status: int, payload: object, snippet: str, verb: str = "查询") -> str:
    """由 GoDaddy 错误体提取 message/code；不含 URL 与密钥。"""
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("code") or "").strip()
    if not detail:
        detail = snippet
    return f"{verb}失败（HTTP {status}）" + (f"：{detail}" if detail else "")


def _clean_ttl(value) -> int:
    """GoDaddy 最小 TTL 为 600；非法值或过小一律回落到 600。"""
    try:
        ttl = int(str(value).strip())
    except (TypeError, ValueError):
        return 600
    return ttl if ttl >= 600 else 600


def _record_body(params: dict) -> dict:
    """把表单参数收敛成 GoDaddy 记录体（type / name / data / ttl）。"""
    return {
        "type": (params.get("record_type") or "A").strip().upper(),
        "name": (params.get("name") or "@").strip(),
        "data": (params.get("data") or "").strip(),
        "ttl": _clean_ttl(params.get("ttl")),
    }


def _secrets(data: dict) -> tuple[str, ...]:
    """本凭据的秘密字段，用于错误文本净化。"""
    return ((data.get("api_secret") or "").strip(), (data.get("api_key") or "").strip())


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在上游返回里的密钥替换掉，任何路径都不回显凭据。

    只替换长度 ≥ 6 的密钥：与 backend/api.py 的净化口径一致，否则短密钥
    会把域名、记录值里的常见字符整片打码成 ``***``。
    """
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _redact_result(result: dict, secrets: tuple[str, ...]) -> dict:
    """净化出口结果：上游把密钥回显进错误体时也不外泄。"""
    for key in ("output", "error"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = _redact(value, *secrets)
    return result


def _execute(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：域名 / 解析记录列表（读），新增 / 修改解析（写）。"""
    data = data or {}
    params = params or {}
    try:
        if action_id == "list_domains":
            status, payload, snippet = _godaddy_get("/domains", data, limit=50)
            if status != 200:
                return {"ok": False, "error": _error_message(status, payload, snippet)}
            domains = payload if isinstance(payload, list) else []
            rows = []
            for item in domains:
                if not isinstance(item, dict):
                    continue
                rows.append({
                    "name": str(item.get("domain") or ""),
                    "status": str(item.get("status") or ""),
                    "expires": str(item.get("expires") or "")[:10],
                })
            return {"ok": True, "rows": rows, "total": len(rows)}

        if action_id == "list_records":
            domain = (params.get("domain") or "").strip()
            if not domain:
                return {"ok": False, "error": "请先选择域名（从「域名列表」行内的「解析」进入）"}
            status, payload, snippet = _godaddy_get(f"/domains/{domain}/records", data)
            if status != 200:
                return {"ok": False, "error": _error_message(status, payload, snippet)}
            records = payload if isinstance(payload, list) else []
            rows = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                rows.append({
                    "name": str(record.get("name") or ""),
                    "type": str(record.get("type") or "").upper(),
                    "data": str(record.get("data") or ""),
                    "ttl": str(record.get("ttl") or ""),
                    # domain 不进列，供行内「修改」回填
                    "domain": domain,
                })
            return {"ok": True, "rows": rows, "total": len(rows)}

        if action_id == "add_record":
            domain = (params.get("domain") or "").strip()
            if not domain:
                return {"ok": False, "error": "域名不能为空"}
            body = _record_body(params)
            if not body["data"]:
                return {"ok": False, "error": "记录值不能为空"}
            # PATCH 是「追加」，不会动到同主机名下的其它记录；PUT 才是全量替换
            status, payload, snippet = _godaddy_write(
                "PATCH", f"/domains/{domain}/records", data, [body]
            )
            if status not in (200, 201):
                return {"ok": False, "error": _error_message(status, payload, snippet, verb="新增")}
            return {
                "ok": True,
                "output": f"新增记录成功：{domain} {body['name']} {body['type']} → {body['data']}",
            }

        if action_id == "edit_record":
            domain = (params.get("domain") or "").strip()
            if not domain:
                return {"ok": False, "error": "域名不能为空"}
            body = _record_body(params)
            if not body["data"]:
                return {"ok": False, "error": "记录值不能为空"}
            # 覆盖路径上的 type/name 定位，body 里只留 data/ttl
            replace = [{"data": body["data"], "ttl": body["ttl"]}]
            status, payload, snippet = _godaddy_write(
                "PUT",
                f"/domains/{domain}/records/{body['type']}/{body['name']}",
                data,
                replace,
            )
            if status != 200:
                return {"ok": False, "error": _error_message(status, payload, snippet, verb="修改")}
            return {
                "ok": True,
                "output": f"修改记录成功：{domain} {body['name']} {body['type']} → {body['data']}",
            }

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作；出口统一做密钥净化，绝不回显 api_key / api_secret。"""
    data = data or {}
    return _redact_result(_execute(action_id, params or {}, data), _secrets(data))


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return "GoDaddy"
    masked = f"{api_key[:4]}..." if len(api_key) > 4 else "已配置"
    return f"GoDaddy | API Key: {masked}"
