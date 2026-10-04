"""Cloudflare API Token / Global API Key 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：支持 token 与 global 两种认证模式，调用只读的
/user 或 /user/tokens/verify 接口，不产生任何资源变更。
仅从 cloud_common 复用通用工具。
"""
from __future__ import annotations

from typing import Any, Optional

from cloud_common import VerifyResult, body_snippet, guarded, make_client, safe_json

# Cloudflare API 基地址
API_BASE = "https://api.cloudflare.com/client/v4"


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _use_global(data: dict) -> Optional[bool]:
    """判定凭据模式：True=Global API Key，False=API Token，None=未提供可用凭据。

    显式 auth_mode 优先；未选择时按已填字段推断。
    """
    auth_mode = _field(data, "auth_mode").lower()
    api_token = _field(data, "api_token")
    email = _field(data, "email")
    global_key = _field(data, "global_api_key")

    if auth_mode == "global":
        return True
    if auth_mode == "token":
        return False
    if api_token:
        return False
    if email and global_key:
        return True
    return None


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []

    use_global = _use_global(data)
    if use_global is None:
        errors.append("请选择认证方式，并填写对应的凭据（API Token，或 Email + Global API Key）")
    elif use_global:
        if not _field(data, "email"):
            errors.append("Global API Key 模式下 Email 不能为空")
        if not _field(data, "global_api_key"):
            errors.append("Global API Key 模式下 Global API Key 不能为空")
    elif not _field(data, "api_token"):
        errors.append("API Token 模式下 API Token 不能为空")

    return errors


@guarded
def verify(data):
    use_global = _use_global(data)
    if use_global is None:
        return VerifyResult(
            status="invalid",
            message="未提供可用的 Cloudflare 凭据（需要 API Token，或 Email + Global API Key）",
        )

    if use_global:
        email = _field(data, "email")
        global_key = _field(data, "global_api_key")
        if not email or not global_key:
            return VerifyResult(
                status="invalid", message="Global API Key 模式需要同时填写 Email 与 Global API Key"
            )
        url = f"{API_BASE}/user"
        headers = {"X-Auth-Email": email, "X-Auth-Key": global_key}
        mode = "global"
    else:
        api_token = _field(data, "api_token")
        if not api_token:
            return VerifyResult(status="invalid", message="API Token 模式下 API Token 不能为空")
        url = f"{API_BASE}/user/tokens/verify"
        headers = {"Authorization": f"Bearer {api_token}"}
        mode = "token"

    with make_client() as client:
        resp = client.get(url, headers=headers)

    status = resp.status_code
    payload = safe_json(resp) or {}
    success = payload.get("success")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

    if success is True:
        info: dict[str, Any] = {}
        if mode == "token":
            if result.get("id"):
                info["token_id"] = result["id"]
            if result.get("status"):
                info["token_status"] = result["status"]
        else:
            if result.get("email"):
                info["email"] = result["email"]
            if result.get("id"):
                info["user_id"] = result["id"]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 认证失败：success=false（通常 HTTP 401/403）
    if success is False or status in (401, 403):
        errors = payload.get("errors")
        detail = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = errors[0].get("message") or ""
        message = "凭证无效" if not detail else f"凭证无效: {detail}"
        return VerifyResult(status="invalid", message=message)

    return _unconfirmed(status, body_snippet(resp))


# ── 可选操作：站点 / DNS 记录（查询只读，记录维护为写操作）──

class _ActionError(Exception):
    """操作边界内可预期的失败：消息直接反馈给用户（调用前已做密钥净化）。"""


def _cf_headers(data: dict) -> Optional[dict]:
    """按凭据模式构造认证头；凭据不完整时返回 None（绝不回显密钥）。"""
    use_global = _use_global(data)
    if use_global is None:
        return None
    if use_global:
        email = _field(data, "email")
        global_key = _field(data, "global_api_key")
        if not email or not global_key:
            return None
        return {"X-Auth-Email": email, "X-Auth-Key": global_key}
    api_token = _field(data, "api_token")
    if not api_token:
        return None
    return {"Authorization": f"Bearer {api_token}"}


def _secrets(data: dict) -> tuple[str, ...]:
    """本凭据中的全部秘密字段，用于错误文本净化。"""
    return (_field(data, "api_token"), _field(data, "global_api_key"))


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _api_error(payload: dict, status: int) -> str:
    """从 Cloudflare 响应提取错误描述（只取 message，不带请求 URL）。"""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        detail = str(errors[0].get("message") or "").strip()
        if detail:
            return detail
    return f"HTTP {status}"


def _is_zone_id(value: str) -> bool:
    """Cloudflare Zone ID 为 32 位十六进制串，可据此省掉一次名称解析。"""
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


def _ttl(value: str) -> int:
    """TTL 解析：1=自动；非法值或越界回落到 1（自动）。"""
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        return 1
    return ttl if ttl == 1 or 60 <= ttl <= 86400 else 1


def _resolve_zone(client, headers: dict, zone: str) -> tuple[str, str]:
    """把站点名称解析为 (zone_id, zone_name)；入参已是 Zone ID 时直接返回。"""
    if _is_zone_id(zone):
        return zone, ""
    resp = client.get(f"{API_BASE}/zones", headers=headers, params={"name": zone, "per_page": 1})
    payload = safe_json(resp) or {}
    if payload.get("success") is not True:
        raise _ActionError(f"查询站点失败：{_api_error(payload, resp.status_code)}")
    result = payload.get("result")
    if not isinstance(result, list) or not result:
        raise _ActionError(f"未找到站点：{zone}")
    item = result[0] if isinstance(result[0], dict) else {}
    zone_id = str(item.get("id") or "")
    if not zone_id:
        raise _ActionError(f"未找到站点：{zone}")
    return zone_id, str(item.get("name") or zone)


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供站点查询与 DNS 记录查看 / 维护）。"""
    if _cf_headers(data or {}) is None:
        return []
    return [
        {
            "id": "list_zones",
            "name": "查看站点（Zones）",
            "kind": "form",
            "submit": "查询",
            "fields": [],
        },
        {
            "id": "list_dns_records",
            "name": "查看 DNS 记录",
            "kind": "form",
            "submit": "查询",
            "fields": [
                {
                    "key": "zone",
                    "label": "站点",
                    "type": "text",
                    "placeholder": "站点名称（example.com）或 Zone ID",
                    "required": True,
                },
            ],
        },
        {
            "id": "set_dns_record",
            "name": "新增 / 修改 DNS 记录",
            "kind": "form",
            "submit": "提交",
            "fields": [
                {
                    "key": "zone",
                    "label": "站点",
                    "type": "text",
                    "placeholder": "站点名称 或 Zone ID",
                    "required": True,
                },
                {"key": "record_id", "label": "记录 ID", "type": "text", "placeholder": "留空=新增；填写=修改该记录"},
                {
                    "key": "record_type",
                    "label": "记录类型",
                    "type": "select",
                    "options": ["A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV"],
                },
                {
                    "key": "name",
                    "label": "记录名",
                    "type": "text",
                    "placeholder": "完整记录名，如 www.example.com",
                    "required": True,
                },
                {"key": "content", "label": "记录值", "type": "text", "placeholder": "如 1.2.3.4", "required": True},
                {"key": "ttl", "label": "TTL", "type": "text", "default": "1", "placeholder": "1=自动，或 60-86400"},
                {
                    "key": "proxied",
                    "label": "代理",
                    "type": "select",
                    "options": ["false", "true"],
                    "default": "false",
                },
            ],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看站点 / DNS 记录（只读），或新增 / 修改 DNS 记录（写）。"""
    data = data or {}
    params = params or {}
    secrets = _secrets(data)
    try:
        headers = _cf_headers(data)
        if headers is None:
            return {"ok": False, "error": "凭据不完整，无法调用 Cloudflare API"}

        if action_id == "list_zones":
            with make_client() as client:
                resp = client.get(f"{API_BASE}/zones", headers=headers, params={"per_page": 50})
            payload = safe_json(resp) or {}
            if payload.get("success") is not True:
                raise _ActionError(_api_error(payload, resp.status_code))
            zones = payload.get("result") if isinstance(payload.get("result"), list) else []
            lines = [f"共 {len(zones)} 个站点：", ""]
            for zone in zones:
                if not isinstance(zone, dict):
                    continue
                lines.append(str(zone.get("name") or "-"))
                lines.append(f"    id={zone.get('id', '-')}  状态={zone.get('status', '-')}")
            return {"ok": True, "output": "\n".join(lines)}

        if action_id in ("list_dns_records", "set_dns_record"):
            zone_input = _field(params, "zone")
            if not zone_input:
                return {"ok": False, "error": "请填写站点名称或 Zone ID"}

            with make_client() as client:
                zone_id, zone_name = _resolve_zone(client, headers, zone_input)

                if action_id == "list_dns_records":
                    resp = client.get(
                        f"{API_BASE}/zones/{zone_id}/dns_records",
                        headers=headers,
                        params={"per_page": 100},
                    )
                    payload = safe_json(resp) or {}
                    if payload.get("success") is not True:
                        raise _ActionError(_api_error(payload, resp.status_code))
                    records = payload.get("result") if isinstance(payload.get("result"), list) else []
                    lines = [f"{zone_name or zone_id} 共 {len(records)} 条 DNS 记录：", ""]
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        proxied = "是" if record.get("proxied") else "否"
                        lines.append(
                            f"{str(record.get('type', '')):<6} {record.get('name', '')} → {record.get('content', '')}"
                        )
                        lines.append(
                            f"    TTL={record.get('ttl', '')}  代理={proxied}  id={record.get('id', '')}"
                        )
                    return {"ok": True, "output": "\n".join(lines)}

                # set_dns_record：record_id 留空=新增（POST），填写=修改（PUT）
                record_type = (_field(params, "record_type") or "A").upper()
                name = _field(params, "name")
                content = _field(params, "content")
                if not name or not content:
                    return {"ok": False, "error": "记录名与记录值不能为空"}
                body: dict[str, Any] = {
                    "type": record_type,
                    "name": name,
                    "content": content,
                    "ttl": _ttl(_field(params, "ttl")),
                }
                # proxied 仅对 A/AAAA/CNAME 生效，其它类型带上会被接口直接拒绝
                if record_type in ("A", "AAAA", "CNAME"):
                    body["proxied"] = _field(params, "proxied").lower() == "true"

                record_id = _field(params, "record_id")
                if record_id:
                    resp = client.put(
                        f"{API_BASE}/zones/{zone_id}/dns_records/{record_id}",
                        headers=headers,
                        json=body,
                    )
                    verb = f"修改记录 {record_id}"
                else:
                    resp = client.post(
                        f"{API_BASE}/zones/{zone_id}/dns_records",
                        headers=headers,
                        json=body,
                    )
                    verb = "新增记录"
                payload = safe_json(resp) or {}
                if payload.get("success") is not True:
                    raise _ActionError(f"{verb}失败：{_api_error(payload, resp.status_code)}")
                result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                return {
                    "ok": True,
                    "output": f"{verb}成功：{record_type} {name} → {content}（id={result.get('id', '')}）",
                }

        return {"ok": False, "error": "未知操作"}
    except _ActionError as exc:
        return {"ok": False, "error": _redact(str(exc), *secrets)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    data = data or {}
    # 展示用推断：显式 global 或「只填了 Global API Key」都按 global 展示
    auth_mode = _field(data, "auth_mode").lower()
    is_global = auth_mode == "global" or (
        auth_mode != "token"
        and bool(_field(data, "global_api_key"))
        and not _field(data, "api_token")
    )
    mode = "Global API Key" if is_global else "API Token"
    return f"Cloudflare | {mode}"
