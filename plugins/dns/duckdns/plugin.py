"""DuckDNS Token 验证插件（plugins/dns/ 类别下的独立子插件）。

参考文档：https://www.duckdns.org/spec.jsp
"""
import time

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json

_DUCKDNS_FQDN_SUFFIX = ".duckdns.org"
_DUCKDNS_TEST_IP = "1.1.1.1"
_DOH_URL = "https://1.1.1.1/dns-query"


def _normalize_duckdns_domain(raw: str) -> str:
    """把用户填写的域名归一化为 DuckDNS 的 domains 参数（子域名部分）。

    允许 "xxx"、"xxx.duckdns.org"、"https://xxx.duckdns.org/" 等写法。
    """
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = value.split("//")[-1].split("/")[0].split("?")[0]
    suffix = ".duckdns.org"
    if value.endswith(suffix):
        value = value[: -len(suffix)]
    return value


def _doh_resolve_a(fqdn: str) -> str | None:
    """用 Cloudflare DoH 查询 A 记录，返回首个 IPv4；失败或无记录返回 None。"""
    try:
        with make_client() as client:
            resp = client.get(
                _DOH_URL,
                params={"name": fqdn, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 解析失败按无记录处理
        return None
    for ans in (payload or {}).get("Answer") or []:
        if ans.get("type") == 1 and ans.get("data"):
            return str(ans["data"])
    return None


def _duckdns_update(domain: str, token: str, ip: str | None) -> tuple[bool, str]:
    """调用 DuckDNS update 接口；返回 (是否 OK, 状态行)。

    ip 为 None 时由 DuckDNS 使用请求来源 IP；给定 ip 时写入该 IP。
    """
    params: dict[str, str] = {"domains": domain, "token": token, "txt": "", "verbose": "true"}
    if ip:
        params["ip"] = ip
    with make_client() as client:
        resp = client.get("https://www.duckdns.org/update", params=params)
    text = (resp.text or "").strip()
    status_line = text.splitlines()[0].strip().upper() if text else ""
    return status_line == "OK", status_line


def _verify_duckdns(data: dict) -> VerifyResult:
    """DuckDNS：无独立只读校验端点，用「写入测试 IP → 确认生效 → 还原」判定。

    流程（会真实修改解析记录，故该插件在 manifest 中禁用批量验证）：
      1. 读取当前 A 记录（Cloudflare DoH）作为原始值；
      2. 用 token 把 A 记录写入 1.1.1.1；
      3. 轮询确认解析是否变为 1.1.1.1（证明 token 有效且写入生效）；
      4. 无论结果如何，最后都把 A 记录还原为原始值。
    """
    token = (data.get("token") or "").strip()
    domain = _normalize_duckdns_domain(data.get("domain") or "")
    if not domain:
        return VerifyResult(status="invalid", message="缺少域名：DuckDNS 需提供自有域名才能验证")
    fqdn = f"{domain}{_DUCKDNS_FQDN_SUFFIX}"

    original = _doh_resolve_a(fqdn)
    if not original:
        return VerifyResult(
            status="error",
            message=f"无法读取 {fqdn} 当前 A 记录，为避免残留测试 IP 已中止（未做任何写入）",
        )

    ok, status_line = _duckdns_update(domain, token, _DUCKDNS_TEST_IP)
    if not ok:
        # DuckDNS 拒绝（KO）→ token 或域名不正确，未改动记录
        return VerifyResult(
            status="invalid",
            message=f"凭证无效：Token 或域名不正确（DuckDNS 返回 {status_line or '空响应'}）",
        )

    observed = False
    deadline = time.time() + 45
    while time.time() < deadline:
        if _doh_resolve_a(fqdn) == _DUCKDNS_TEST_IP:
            observed = True
            break
        time.sleep(5)

    # 无论验证结果如何，都还原原始解析，并确认还原是否生效
    restored = False
    try:
        restored, _ = _duckdns_update(domain, token, original)
        if restored:
            for _ in range(3):
                if _doh_resolve_a(fqdn) == original:
                    break
                time.sleep(3)
    except Exception:  # noqa: BLE001 - 还原失败需要在结果里显式告知
        restored = False

    info = {
        "domain": fqdn,
        "original_ip": original,
        "tested_ip": _DUCKDNS_TEST_IP,
        "restored": restored,
    }
    if not restored:
        return VerifyResult(
            status="valid",
            message=f"凭证有效，但【还原失败】！请手动将 {fqdn} 改回 {original}",
            account_info=info,
        )
    if observed:
        return VerifyResult(
            status="valid",
            message=f"凭证有效（已写入 {_DUCKDNS_TEST_IP} 并确认生效，随后已还原为 {original}）",
            account_info=info,
        )
    return VerifyResult(
        status="valid",
        message=f"凭证有效（DuckDNS 已接受写入，但本地 DNS 未及时观察到；已还原为 {original}）",
        account_info=info,
    )


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("token") or "").strip():
        errors.append("DuckDNS Token 不能为空")
    if not (data.get("domain") or "").strip():
        errors.append("域名不能为空")
    return errors


@guarded
def verify(data):
    """验证凭证是否有效。保证不抛出异常。"""
    data = data or {}
    missing = validate(data)
    if missing:
        return VerifyResult(status="invalid", message="；".join(missing))
    return _verify_duckdns(data)


# ── 可选操作：解析查看 / 更新 / 清除 TXT ───────────────

def _doh_resolve_txt(fqdn: str) -> str | None:
    """用 Cloudflare DoH 查询 TXT 记录，返回首个值（已去掉两侧引号）；无记录返回 None。"""
    try:
        with make_client() as client:
            resp = client.get(
                _DOH_URL,
                params={"name": fqdn, "type": "TXT"},
                headers={"Accept": "application/dns-json"},
            )
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 解析失败按无记录处理
        return None
    for ans in (payload or {}).get("Answer") or []:
        if ans.get("type") == 16 and ans.get("data"):
            return str(ans["data"]).strip().strip('"')
    return None


def _duckdns_clear(domain: str, token: str) -> tuple[bool, str]:
    """清除该子域名的 TXT 记录（DuckDNS 的 clear 语义）；返回 (是否 OK, 状态行)。"""
    with make_client() as client:
        resp = client.get(
            "https://www.duckdns.org/update",
            params={"domains": domain, "token": token, "clear": "true"},
        )
    text = (resp.text or "").strip()
    status_line = text.splitlines()[0].strip().upper() if text else ""
    return status_line == "OK", status_line


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 token 与域名时提供解析查看 / 更新 / 清除）。"""
    data = data or {}
    if not ((data.get("token") or "").strip() and _normalize_duckdns_domain(data.get("domain") or "")):
        return []
    domain = _normalize_duckdns_domain(data.get("domain") or "")
    return [
        {
            "id": "view_records",
            "name": "当前解析",
            "kind": "list",
            "columns": [
                {"key": "type", "label": "类型", "type": "tag", "width": 90},
                {"key": "name", "label": "域名", "mono": True},
                {"key": "value", "label": "记录值", "mono": True},
            ],
            "row_actions": [
                # 行内「修改」把该行域名预填进 update_record（domain 不在列里，仅供模板取值）
                {"id": "update_record", "label": "修改", "params": {"domain": "$row.domain"}},
            ],
            "empty_text": "未查询到解析记录",
        },
        {
            "id": "update_record",
            "name": "更新解析",
            "kind": "form",
            "submit": "更新",
            "write": True,
            "fields": [
                {
                    "key": "domain",
                    "label": "域名",
                    "type": "text",
                    "required": True,
                    "default": domain,
                    "placeholder": "子域名，如 myhome（无需 .duckdns.org）",
                },
                {
                    "key": "ip",
                    "label": "IP 地址",
                    "type": "text",
                    "placeholder": "留空 = 使用当前公网 IP",
                },
            ],
        },
        {
            "id": "clear_txt",
            "name": "清除 TXT",
            "kind": "form",
            "submit": "清除",
            "write": True,
            "fields": [
                {
                    "key": "domain",
                    "label": "域名",
                    "type": "text",
                    "required": True,
                    "default": domain,
                    "placeholder": "子域名，如 myhome（无需 .duckdns.org）",
                },
            ],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：查看当前解析（DoH 只读）/ 更新解析 / 清除 TXT。"""
    data = data or {}
    params = params or {}
    try:
        if action_id == "view_records":
            domain = _normalize_duckdns_domain(params.get("domain") or data.get("domain") or "")
            if not domain:
                return {"ok": False, "error": "请填写域名"}
            fqdn = f"{domain}{_DUCKDNS_FQDN_SUFFIX}"
            ip = _doh_resolve_a(fqdn)
            txt = _doh_resolve_txt(fqdn)
            rows = [
                {"type": "A", "name": fqdn, "value": ip or "—", "domain": domain},
                {"type": "TXT", "name": fqdn, "value": txt or "—", "domain": domain},
            ]
            return {"ok": True, "rows": rows, "total": len(rows)}

        if action_id == "update_record":
            domain = _normalize_duckdns_domain(params.get("domain") or data.get("domain") or "")
            if not domain:
                return {"ok": False, "error": "请填写域名"}
            ip = (params.get("ip") or "").strip()
            ok, status_line = _duckdns_update(domain, (data.get("token") or "").strip(), ip or None)
            if not ok:
                return {"ok": False, "error": f"DuckDNS 返回 {status_line or '空响应'}（更新未生效）"}
            target = ip or "当前公网 IP"
            return {"ok": True, "output": f"已更新 {domain}{_DUCKDNS_FQDN_SUFFIX} → {target}"}

        if action_id == "clear_txt":
            domain = _normalize_duckdns_domain(params.get("domain") or data.get("domain") or "")
            if not domain:
                return {"ok": False, "error": "请填写域名"}
            ok, status_line = _duckdns_clear(domain, (data.get("token") or "").strip())
            if not ok:
                return {"ok": False, "error": f"DuckDNS 返回 {status_line or '空响应'}（清除未生效）"}
            return {"ok": True, "output": f"已清除 {domain}{_DUCKDNS_FQDN_SUFFIX} 的 TXT 记录"}

        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    domain = _normalize_duckdns_domain(data.get("domain") or "")
    return f"DuckDNS | {domain}.duckdns.org" if domain else "DuckDNS"
