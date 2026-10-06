"""DNSPod API Token 验证插件（plugins/dns/ 类别下的独立子插件）。

操作（凭据含 dp_id / dp_key 时提供）：
- ``list_domains``（列表）域名列表，行内可下钻 ``list_records``（解析记录）
  或打开 ``add_record``（新增解析）并自动带域名；
- ``list_records``（列表）指定域名的解析记录，行内 ``edit_record``（修改解析）；
- ``add_record`` / ``edit_record``（表单，写操作）新增 / 修改解析记录。

刻意不提供删除解析：破坏性且不属于本插件的验证目标。

参考文档：https://support.dnspod.cn/api/domains-list/
"""
from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json

# DNSPod 明确的鉴权/账号类失败状态码：
# "-1" 登录失败、"-2" 需要实名认证、"-3" 缺少参数或 token 非法、
# "3" 域名不存在、"6" 无效的登录 token / 用户不存在、"85" 用户被封禁。
# 命中这些码即判定凭证无效，其余非 "1" 的状态码归入 error（无法确认）。
_DNSPOD_AUTH_FAIL_CODES = {"3", "6", "-1", "-2", "-3", "85"}


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _verify_dnspod(data: dict) -> VerifyResult:
    """DNSPod：用 login_token 调用 Domain.List，检查 status.code。

    code == "1" 表示 Token 有效，并返回名下域名列表用于统计数量。
    """
    dp_id = (data.get("dp_id") or "").strip()
    dp_key = (data.get("dp_key") or "").strip()

    with make_client() as client:
        resp = client.post(
            "https://dnsapi.cn/Domain.List",
            data={
                "login_token": f"{dp_id},{dp_key}",
                "format": "json",
                "lang": "cn",
                "error_on_empty": "no",
            },
        )

    payload = safe_json(resp)
    if payload is None:
        # 非 JSON 响应（如网关错误页）视为无法确认
        return VerifyResult(
            status="error",
            message=f"响应非 JSON (HTTP {resp.status_code}): {body_snippet(resp)}",
        )

    status_info = payload.get("status")
    status_info = status_info if isinstance(status_info, dict) else {}
    code = str(status_info.get("code", "")).strip()
    api_message = str(status_info.get("message", "")).strip()

    if code == "1":
        info = payload.get("info")
        account_info = {"domain_count": len(info)} if isinstance(info, list) else None
        return VerifyResult(status="valid", message="凭证有效", account_info=account_info)

    if code in _DNSPOD_AUTH_FAIL_CODES:
        message = f"凭证无效: {api_message}" if api_message else f"凭证无效 (code {code})"
        return VerifyResult(status="invalid", message=message)

    if not code:
        return _unconfirmed(resp.status_code, body_snippet(resp))

    detail = f"{api_message}" if api_message else f"code {code}"
    return VerifyResult(status="error", message=f"无法确认凭证状态 ({detail})")


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("dp_id") or "").strip():
        errors.append("DNSPod ID 不能为空")
    if not (data.get("dp_key") or "").strip():
        errors.append("DNSPod API Token 不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_dnspod(data)


# ── 可选操作：域名 / 解析记录管理 ───────────────────────

_DNSPOD_API = "https://dnsapi.cn"

# 记录类型与线路的可选值（线路用 DNSPod 的中文名，接口直接接受）
_RECORD_TYPES = ["A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV"]
_RECORD_LINES = ["默认", "电信", "联通", "移动", "境外", "教育网"]

# 域名状态码 → 中文，未知状态原样显示
_DOMAIN_STATUS = {"enable": "正常", "pause": "暂停", "spam": "封禁", "lock": "锁定"}


def _login_token(data: dict) -> str:
    return f"{(data.get('dp_id') or '').strip()},{(data.get('dp_key') or '').strip()}"


def _record_fields(with_id: bool) -> list[dict]:
    """新增 / 修改解析记录的表单字段（with_id=True 时带记录 ID，供修改用）。"""
    fields: list[dict] = [
        {"key": "domain", "label": "域名", "type": "text", "required": True, "placeholder": "example.com"},
    ]
    if with_id:
        fields.append({
            "key": "record_id",
            "label": "记录 ID",
            "type": "text",
            "required": True,
            "placeholder": "要修改的记录 ID",
        })
    fields += [
        {"key": "sub_domain", "label": "主机记录", "type": "text", "default": "@", "placeholder": "@ / www / *"},
        {"key": "record_type", "label": "记录类型", "type": "select", "options": _RECORD_TYPES, "default": "A"},
        {"key": "record_line", "label": "线路", "type": "select", "options": _RECORD_LINES, "default": "默认"},
        {"key": "value", "label": "记录值", "type": "text", "required": True, "placeholder": "如 1.2.3.4"},
        {"key": "ttl", "label": "TTL", "type": "text", "default": "600"},
    ]
    return fields


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 dp_id / dp_key 时提供域名与解析管理）。"""
    data = data or {}
    if not (data.get("dp_id") and data.get("dp_key")):
        return []
    return [
        {
            "id": "list_domains",
            "name": "域名列表",
            "kind": "list",
            "columns": [
                {"key": "name", "label": "域名", "mono": True},
                {"key": "records", "label": "记录数", "width": 90},
                {"key": "grade", "label": "套餐", "width": 130},
                {"key": "status_text", "label": "状态", "width": 90},
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
                {"key": "value", "label": "记录值", "mono": True},
                {"key": "line", "label": "线路", "width": 90},
                {"key": "ttl", "label": "TTL", "width": 90},
            ],
            # 行内「修改」把整条记录预填进 edit_record 表单（id / domain 不在列里，仅供模板取值）
            "row_actions": [
                {
                    "id": "edit_record",
                    "label": "修改",
                    "params": {
                        "domain": "$row.domain",
                        "record_id": "$row.id",
                        "sub_domain": "$row.name",
                        "record_type": "$row.type",
                        "record_line": "$row.line",
                        "value": "$row.value",
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
            "fields": _record_fields(with_id=False),
        },
        {
            "id": "edit_record",
            "name": "修改解析",
            "kind": "form",
            "submit": "保存",
            "write": True,
            "fields": _record_fields(with_id=True),
        },
    ]


def _dnspod_post(path: str, data: dict, **payload) -> dict:
    body = {"login_token": _login_token(data), "format": "json", "lang": "cn", **payload}
    with make_client() as client:
        resp = client.post(f"{_DNSPOD_API}/{path}", data=body)
    return safe_json(resp) or {}


def _status(payload: dict) -> tuple[str, str]:
    info = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    return str(info.get("code", "")), str(info.get("message", ""))


def _fail(message: str) -> dict:
    """统一失败结构（错误文本会原样展示给用户，出口处还会过一遍密钥净化）。"""
    return {"ok": False, "error": message}


def _secrets(data: dict) -> tuple[str, ...]:
    """本凭据的秘密字段，用于错误文本净化。"""
    return ((data.get("dp_key") or "").strip(),)


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
    """净化出口结果：上游把 token 回显进 status.message 时也不外泄。"""
    for key in ("output", "error"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = _redact(value, *secrets)
    return result


def _write_record(action_id: str, params: dict, data: dict) -> dict:
    """新增（``Record.Create``）或修改（``Record.Modify``）一条解析记录。"""
    common = {
        "domain": (params.get("domain") or "").strip(),
        "sub_domain": (params.get("sub_domain") or "@").strip(),
        "record_type": (params.get("record_type") or "A").strip().upper(),
        "record_line": (params.get("record_line") or "默认").strip(),
        "value": (params.get("value") or "").strip(),
        "ttl": (params.get("ttl") or "600").strip(),
    }
    if not common["domain"] or not common["value"]:
        return _fail("域名与记录值不能为空")

    record_id = (params.get("record_id") or "").strip()
    if action_id == "edit_record":
        if not record_id:
            return _fail("缺少记录 ID，无法修改（请从解析记录列表行内的「修改」进入）")
        payload = _dnspod_post("Record.Modify", data, record_id=record_id, **common)
        verb = "修改"
    else:
        payload = _dnspod_post("Record.Create", data, **common)
        verb = "新增"

    code, message = _status(payload)
    if code != "1":
        return _fail(f"{verb}解析记录失败（code {code}）：{message}")
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    return {
        "ok": True,
        "output": f"{verb}解析记录成功：{common['sub_domain']} {common['record_type']} → "
                  f"{common['value']}（id={record.get('id', '')}）",
    }


def _execute(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：域名 / 解析记录列表（读），新增 / 修改解析（写）。"""
    data = data or {}
    params = params or {}
    try:
        if action_id == "list_domains":
            payload = _dnspod_post("Domain.List", data)
            code, message = _status(payload)
            if code != "1":
                return _fail(f"查询域名失败（code {code}）：{message}")
            # DNSPod 的 Domain.List 把域名数组放在 "domains"，"info" 是统计信息（dict，不是列表）。
            # 曾误读 info 导致域名列表永远为空——这里必须取 domains。
            domains = payload.get("domains")
            domains = domains if isinstance(domains, list) else []
            rows = []
            for item in domains:
                if not isinstance(item, dict):
                    continue
                raw_status = str(item.get("status") or "")
                rows.append({
                    "name": str(item.get("name") or ""),
                    "records": str(item.get("records") or item.get("record_count") or ""),
                    "grade": str(item.get("grade_title") or item.get("grade") or ""),
                    "status_text": _DOMAIN_STATUS.get(raw_status, raw_status),
                    # id 不进列，留作后续行内动作取用
                    "id": str(item.get("id") or ""),
                })
            return {"ok": True, "rows": rows, "total": len(rows)}

        if action_id == "list_records":
            domain = (params.get("domain") or "").strip()
            if not domain:
                return _fail("请先选择域名（从「域名列表」行内的「解析」进入）")
            payload = _dnspod_post("Record.List", data, domain=domain)
            code, message = _status(payload)
            if code != "1":
                return _fail(f"查询解析失败（code {code}）：{message}")
            records = payload.get("records")
            records = records if isinstance(records, list) else []
            rows = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                rows.append({
                    "name": str(record.get("name") or ""),
                    "type": str(record.get("type") or "").upper(),
                    "value": str(record.get("value") or ""),
                    "line": str(record.get("line") or ""),
                    "ttl": str(record.get("ttl") or ""),
                    # id / domain 不进列，供行内「修改」回填
                    "id": str(record.get("id") or ""),
                    "domain": domain,
                })
            return {"ok": True, "rows": rows, "total": len(rows)}

        if action_id in ("add_record", "edit_record"):
            return _write_record(action_id, params, data)

        return _fail("未知操作")
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return _fail(f"执行异常：{type(exc).__name__}")


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作；出口统一做密钥净化，绝不回显 dp_key。"""
    data = data or {}
    return _redact_result(_execute(action_id, params or {}, data), _secrets(data))


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    dp_id = (data.get("dp_id") or "").strip()
    return f"DNSPod | ID: {dp_id}" if dp_id else "DNSPod"
