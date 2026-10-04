"""DNSPod API Token 验证插件（plugins/dns/ 类别下的独立子插件）。"""
from dns_common import VerifyResult, body_snippet, guarded, make_client, safe_json

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


# ── 可选操作：查看 / 设置解析记录 ───────────────────────

_DNSPOD_API = "https://dnsapi.cn"


def _login_token(data: dict) -> str:
    return f"{(data.get('dp_id') or '').strip()},{(data.get('dp_key') or '').strip()}"


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 dp_id / dp_key 时提供解析管理）。"""
    data = data or {}
    if not (data.get("dp_id") and data.get("dp_key")):
        return []
    return [
        {
            "id": "list_records",
            "name": "查看解析记录",
            "kind": "form",
            "submit": "查询",
            "fields": [
                {"key": "domain", "label": "域名", "type": "text", "placeholder": "example.com"},
            ],
        },
        {
            "id": "set_record",
            "name": "新增 / 修改解析记录",
            "kind": "form",
            "submit": "提交",
            "fields": [
                {"key": "domain", "label": "域名", "type": "text", "placeholder": "example.com"},
                {"key": "record_id", "label": "记录 ID", "type": "text", "placeholder": "留空=新增；填写=修改该记录"},
                {"key": "sub_domain", "label": "主机记录", "type": "text", "default": "@", "placeholder": "@ / www / *"},
                {"key": "record_type", "label": "记录类型", "type": "select", "options": ["A", "AAAA", "CNAME", "TXT", "MX", "NS"]},
                {"key": "record_line", "label": "线路", "type": "select", "options": ["默认", "电信", "联通", "移动", "境外"]},
                {"key": "value", "label": "记录值", "type": "text", "placeholder": "如 1.2.3.4"},
                {"key": "ttl", "label": "TTL", "type": "text", "default": "600"},
            ],
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


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看 / 新增 / 修改 DNSPod 解析记录。"""
    data = data or {}
    params = params or {}
    domain = (params.get("domain") or "").strip()
    try:
        if action_id == "list_records":
            if not domain:
                return {"ok": False, "error": "请填写域名"}
            payload = _dnspod_post("Record.List", data, domain=domain)
            code, message = _status(payload)
            if code != "1":
                return {"ok": False, "error": f"查询失败（code {code}）：{message}"}
            records = payload.get("records") or []
            lines = [f"{domain} 共 {len(records)} 条解析：", ""]
            for r in records:
                lines.append(
                    f"[{r.get('id')}] {str(r.get('name', '')):<10} {str(r.get('type', '')):<6} "
                    f"{r.get('value', '')}  TTL={r.get('ttl', '')} 线路={r.get('line', '')}"
                )
            return {"ok": True, "output": "\n".join(lines)}

        if action_id == "set_record":
            common = {
                "domain": domain,
                "sub_domain": (params.get("sub_domain") or "@").strip(),
                "record_type": (params.get("record_type") or "A").strip(),
                "record_line": (params.get("record_line") or "默认").strip(),
                "value": (params.get("value") or "").strip(),
                "ttl": (params.get("ttl") or "600").strip(),
            }
            if not common["domain"] or not common["value"]:
                return {"ok": False, "error": "域名与记录值不能为空"}
            record_id = (params.get("record_id") or "").strip()
            if record_id:
                payload = _dnspod_post("Record.Modify", data, record_id=record_id, **common)
                verb = f"修改记录 {record_id}"
            else:
                payload = _dnspod_post("Record.Create", data, **common)
                verb = "新增记录"
            code, message = _status(payload)
            if code != "1":
                return {"ok": False, "error": f"{verb}失败（code {code}）：{message}"}
            rid = (payload.get("record") or {}).get("id", "")
            return {
                "ok": True,
                "output": f"{verb}成功：{common['sub_domain']} {common['record_type']} → "
                          f"{common['value']}（id={rid}）",
            }

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    dp_id = (data.get("dp_id") or "").strip()
    return f"DNSPod | ID: {dp_id}" if dp_id else "DNSPod"
