"""腾讯 CodeBuddy 客户端登录态验证插件（plugins/llm/codebuddy/，自包含实现）。

CodeBuddy 是腾讯的 AI 编程助手（中国站 www.codebuddy.cn / 国际站 www.codebuddy.ai）。
这里的「凭据」不是 API Key，而是**客户端登录态**——两个 Keycloak 风格的 JWT
（``iss`` = ``…/auth/realms/copilot``）。

登录态**按字段分开填**，不要求粘贴整份登录态文件（那样既难维护、又把不必要的字段一起存了）。
字段一一对应登录态文件里的位置：

- ``access_token``（必需）：``auth.accessToken``，本地判过期 + 联网校验都用它；
- ``uid``（必需）：``account.uid``，联网校验请求头 ``X-User-Id`` 的值；
- ``refresh_token``（可选）：``auth.refreshToken``，**只保存、从不使用**；
- ``enterprise_id``（可选）：``account.enterpriseId``，个人账号为空；
- ``domain``（可选）：``auth.domain``，默认 ``www.codebuddy.cn``。

**``refresh_token`` 只存不用**：CodeBuddy 的刷新会**轮换**刷新令牌，属写副作用，
本插件整条验证路径绝不调用任何刷新/轮换接口，全程只发一次只读的额度查询。

验证策略（**全程只读**）：

1. 本地解析 JWT（base64url 解 payload，**不验签**）取出账号信息与到期时间；
2. 联网校验：向上游 ``POST /v2/billing/meter/get-user-resource``（套餐/额度查询，只读）。
   该接口在网关（APISIX）层鉴权——无 token 或伪造 token 一律 401，
   所以它既能证明登录态有效，又能顺带取回套餐名与用量用于「附加信息」；
3. 联网失败（超时 / 无网络 / 未知响应）时退回本地结论，并在 message 里如实标注
   「本地校验（未联网）」。

**绝不回显 accessToken / refreshToken**：message、account_info、actions 的行数据里
都只出现非敏感信息（手机号打码、uid 只取前 8 位）。

**关于「官方有没有只读校验接口」（2026-10-07 逐页核对官方文档后的结论）：**
官方**唯一**公开的 HTTP API 是 **本地 CLI 网关**（``codebuddy --serve`` 起在 127.0.0.1，
``/api/v1/*``，见 https://www.codebuddy.cn/docs/cli/http-api），它用的是**本地生成的口令**
（Bearer / ``X-Access-Token`` / ``gateway_session`` cookie、强制头 ``X-CodeBuddy-Request: 1``），
**不是**本插件手里的 Keycloak access_token，且端点全集里**没有任何** account / quota / billing
端点——所以它既校验不了本凭据、也不提供账号/额度信息。IDE 侧「账号」文档（积分 / 套餐 / 用量）
**全是界面说明、无 REST 接口**。本插件用的 ``/v2/billing/meter/get-user-resource`` 是**私有接口、
官方无文档**（出处为第三方协议转换器 ``IceeAn/codebuddy2api``）。**别再去找「官方只读校验接口」。**

参考文档：https://www.codebuddy.cn/docs/
参考文档：https://www.codebuddy.cn/docs/cli/http-api
参考文档：https://www.codebuddy.cn/docs/ide/Account/credits
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from typing import Optional

import httpx

from backend.plugin_sdk import VerifyResult, guarded, log, make_client, safe_json
from llm_common import plausible_amount

NAME = "腾讯 CodeBuddy"

# ── 上游端点 ────────────────────────────────────────────
# 登录态里的 auth.domain 是「认证域」（Keycloak realm 所在站点），
# 而业务 API 的入口另有其站：中国站走 copilot.tencent.com，国际站走 www.codebuddy.ai
# （来源：第三方协议转换器 IceeAn/codebuddy2api 的默认配置 CODEBUDDY_API_ENDPOINT
#  与 CODEBUDDY_ALLOWED_API_ENDPOINTS，见 docs 记录）。
DEFAULT_ENDPOINT = "https://copilot.tencent.com"
# 登录态里 auth.domain 的缺省值：留空按中国站处理
DEFAULT_DOMAIN = "www.codebuddy.cn"
_ENDPOINT_BY_DOMAIN = {
    "www.codebuddy.cn": "https://copilot.tencent.com",
    "www.codebuddy.ai": "https://www.codebuddy.ai",
}

# 只读的额度查询接口：网关层鉴权，无 token / 假 token 均返回 401。
# 企业账号另有 /v2/billing/meter/get-enterprise-user-usage，本插件不区分。
QUOTA_PATH = "/v2/billing/meter/get-user-resource"
QUOTA_PRODUCT_CODE = "p_tcaca"
QUOTA_RANGE_END = "2127-01-01 00:00:00"

# 伪装成官方 IDE / CLI 的版本号（上游可能据此放行）。CLI 版本对齐 npm 官方包
# `@tencent-ai/codebuddy-code` 的最新稳定版（2026-10-07 时为 2.161.4；旧值 2.107.0 已明显落后，
# 版本头不匹配时腾讯侧一旦做版本门禁，表现会是难以定位的 403/400）。
CLI_VERSION = "2.161.4"
IDE_VERSION = "4.9.13"
USER_AGENT = f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}"


# ── 本地解析 ────────────────────────────────────────────

def _decode_jwt_payload(token: str) -> Optional[dict]:
    """本地解开 JWT 的 payload（base64url，**不验签**），失败返回 None。

    只做格式解析：JWT 是 ``header.payload.signature``，payload 段用 base64url 编码
    且**省略了 padding**，所以要按 4 对齐补齐 ``=`` 再解。任何异常都吞掉返回 None
    ——调用方靠 None 走「无法判定」分支，而不是抛异常。
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if not payload:
        return None
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except Exception:  # noqa: BLE001 - 非法 base64 一律当解析失败
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 非法 JSON 一律当解析失败
        return None
    return data if isinstance(data, dict) else None


def _normalize_epoch(value) -> Optional[float]:
    """把时间戳统一成「秒」。兼容秒（10 位）与毫秒（13 位）两种写法。"""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if num > 1e12:          # 毫秒时间戳（登录态文件里的 expiresAt 常见毫秒写法）
        num /= 1000.0
    return num


def _fmt_time(value) -> str:
    """把 unix 秒格式化成可读的本地时间；非法值返回空串。"""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _human_delta(seconds: float) -> str:
    """把剩余秒数转成「n 天 n 小时」这类人话（用于展示 token 剩余有效期）。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return "已过期"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def _mask_phone(value) -> str:
    """手机号等账号名打码：保留前 3 位与后 4 位（``135****1355``）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 7:
        return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"
    return f"{text[:3]}****{text[-4:]}"


def _short_id(value) -> str:
    """长 ID（uid / sub）只给前 8 位，避免完整标识外泄。"""
    text = str(value or "").strip()
    return f"{text[:8]}…" if len(text) > 8 else text


# ── 分字段读取 ──────────────────────────────────────────

def _read(data: dict) -> dict:
    """把用户**分字段**填的登录态摊平成内部结构（只做取值，不做任何判定）。

    不再要求粘贴整份登录态文件：字段一一对应文件里的位置——

    - ``access_token``：必需（= ``auth.accessToken``，JWT）
    - ``uid``：必需（= ``account.uid``；联网探针请求头 ``X-User-Id`` 的值）
    - ``refresh_token``：可选（= ``auth.refreshToken``）——**只存不用**（见模块说明）
    - ``enterprise_id``：可选（= ``account.enterpriseId``，个人账号为空）
    - ``domain``：可选（= ``auth.domain``），留空取 ``DEFAULT_DOMAIN``

    过期判断只看 access_token 自己的 JWT ``exp``——不再收登录态里的 ``expiresAt``
    （两者同源，多存一个字段没有意义）。
    """
    data = data or {}

    def _text(key: str) -> str:
        value = data.get(key)
        return str(value).strip() if value is not None else ""

    return {
        "access_token": _text("access_token"),
        "refresh_token": _text("refresh_token"),
        "uid": _text("uid"),
        "enterprise_id": _text("enterprise_id"),
        "domain": _text("domain") or DEFAULT_DOMAIN,
    }


# ── 联网：只读额度/套餐查询（同时充当即时鉴权探针） ──────

def _endpoint_for(domain: str) -> str:
    return _ENDPOINT_BY_DOMAIN.get(domain.strip().lower(), DEFAULT_ENDPOINT)


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def _auth_headers(token: str, uid: str, enterprise_id: str, domain: str) -> dict:
    """拼装上游要求的鉴权头（照抄官方 IDE 客户端的行为）。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid or "",
        "X-Domain": domain or "",
        "X-Requested-With": "XMLHttpRequest",
        "X-Product": "SaaS",
        "X-CodeBuddy-Request": "1",
        "X-IDE-Type": "CodeBuddyIDE",
        "X-IDE-Name": "CodeBuddyIDE",
        "X-IDE-Version": IDE_VERSION,
        "X-Product-Version": IDE_VERSION,
        "User-Agent": USER_AGENT,
    }
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def _quota_payload() -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "PageNumber": 1,
        "PageSize": 200,
        "ProductCode": QUOTA_PRODUCT_CODE,
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now,
        "PackageEndTimeRangeEnd": QUOTA_RANGE_END,
    }


def _probe_quota(endpoint: str, token: str, uid: str, enterprise_id: str,
                 candidates: list[str]) -> tuple[str, Optional[dict], str]:
    """向额度接口发一次**只读**查询，兼作鉴权探针。

    返回 ``(结论, 额度数据, 生效的 domain)``，结论取值：

    - ``"ok"``          200 且 ``code == 0`` —— 登录态有效，额度数据一并带回
    - ``"auth_failed"`` 401 / 403 —— 网关拒绝该 token
    - ``"unknown"``     网络异常 / 其它状态码 / 返回体结构不符 —— **不下结论**

    只认这三种，避免把「429 / 5xx / 结构变化」误判成凭据失效。
    """
    last = "unknown"
    for domain in candidates:
        try:
            with make_client() as client:
                resp = client.post(
                    f"{endpoint}{QUOTA_PATH}",
                    headers=_auth_headers(token, uid, enterprise_id, domain),
                    json=_quota_payload(),
                )
        except httpx.HTTPError:
            return "unknown", None, ""        # 网络问题：换域名重试没有意义
        if resp.status_code == 200:
            body = safe_json(resp)
            if isinstance(body, dict) and body.get("code") == 0:
                return "ok", _parse_quota(body), domain
            return "unknown", None, ""        # 200 但业务码未知：不判定
        if resp.status_code in (401, 403):
            last = "auth_failed"
            continue
        return "unknown", None, ""
    return last, None, ""


def _cycle_value(account: dict, field: str) -> Optional[float]:
    """取额度字段：优先 ``*Precise``（高精度），退回普通字段。"""
    for key in (f"{field}Precise", field):
        if key in account:
            value = _normalize_number(account.get(key))
            if value is not None:
                return value
    return None


def _normalize_number(value) -> Optional[float]:
    """把额度数字解析为非负有限浮点；金额类哨兵值交回 plausible_amount 处理。"""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        return None
    return num


def _parse_quota(payload: dict) -> Optional[dict]:
    """从额度响应里抽出套餐列表与汇总用量，结构不符返回 None。"""
    data = payload.get("data")
    envelope = data.get("Response") if isinstance(data, dict) else None
    body = envelope.get("Data") if isinstance(envelope, dict) else data
    accounts = body.get("Accounts") if isinstance(body, dict) else None
    if accounts is None:
        return None
    if not isinstance(accounts, list):
        return None

    packages, total, used = [], 0.0, 0.0
    for account in accounts:
        if not isinstance(account, dict) or account.get("Status") not in (0, None):
            continue
        size = _cycle_value(account, "CycleCapacitySize")
        remain = _cycle_value(account, "CycleCapacityRemain")
        spent = _cycle_value(account, "CycleCapacityUsed")
        if size is None and remain is None and spent is None:
            continue
        name = account.get("PackageName")
        name = name if isinstance(name, str) and name.strip() else "未命名套餐"
        packages.append({
            "name": name,
            "total": size,
            "remaining": remain,
            "used": spent,
        })
        if size is not None and spent is not None:
            total += size
            used += spent
    return {"packages": packages, "total": total, "used": used}


def _plan_and_usage(quota: Optional[dict]) -> tuple[str, Optional[float]]:
    """从额度数据里得出「套餐」展示名与「使用率」（已用 ÷ 总额度）。"""
    if not quota:
        return "", None
    packages = quota.get("packages") or []
    plan = ""
    if packages:
        plan = packages[0]["name"]
        if len(packages) > 1:
            plan = f"{plan} 等 {len(packages)} 个"
    usage = None
    total, used = quota.get("total") or 0.0, quota.get("used") or 0.0
    if total > 0 and used >= 0 and plausible_amount(total) and plausible_amount(used):
        usage = max(0.0, min(1.0, used / total))
    return plan[:40], usage


# ── 展示信息（全部非敏感） ──────────────────────────────

def _account_info(claims: dict, phone: str, uid: str, plan: str = "",
                  usage: Optional[float] = None,
                  access_exp: Optional[float] = None) -> Optional[dict]:
    """组装「附加信息」：只放非敏感字段，绝不出现任何 token。"""
    info: dict = {}
    email = str(claims.get("email") or "").strip()
    if email:
        info["email"] = email
    masked = _mask_phone(phone)
    if masked:
        info["username"] = masked
    if plan:
        info["plan"] = plan
    if usage is not None:
        info["usage_rate"] = round(usage, 4)
    if uid:
        info["account_id"] = uid[:8]
    if access_exp:
        info["expires"] = _fmt_time(access_exp)
    return info or None


# ── 验证 ────────────────────────────────────────────────

def _verify(data: dict) -> VerifyResult:
    auth = _read(data)
    access = auth["access_token"]
    uid = auth["uid"]
    refresh = auth["refresh_token"]
    domain = auth["domain"]
    enterprise_id = auth["enterprise_id"]
    if not access:
        return VerifyResult(status="invalid", message="缺少 access_token（＝登录态里的 auth.accessToken）")
    if not uid:
        return VerifyResult(status="invalid", message="缺少 uid（＝登录态里的 account.uid）")

    claims = _decode_jwt_payload(access) or {}
    refresh_claims = _decode_jwt_payload(refresh) or {}

    phone = str(claims.get("preferred_username") or "").strip()
    now = time.time()
    access_exp = _normalize_epoch(claims.get("exp"))
    refresh_exp = _normalize_epoch(refresh_claims.get("exp"))

    endpoint = _endpoint_for(domain)
    # X-Domain 到底是「认证域」还是「上游 host」两家实现不一致，两个都试一遍。
    candidates = [d for d in (domain, _host_of(endpoint)) if d]
    verdict, quota, _ = _probe_quota(endpoint, access, uid, enterprise_id, candidates)

    if verdict == "ok":
        plan, usage = _plan_and_usage(quota)
        return VerifyResult(
            status="valid",
            message="登录态有效（已联网校验）",
            account_info=_account_info(claims, phone, uid, plan, usage, access_exp),
        )

    if verdict == "auth_failed":
        # 网关明确拒绝：可能只是 accessToken 过期，也可能整个登录态失效。
        if refresh_exp and refresh_exp > now:
            return VerifyResult(
                status="valid",
                message=(f"accessToken 已过期（上游返回 401），但 refreshToken 仍有效至 "
                         f"{_fmt_time(refresh_exp)}，在客户端重新请求即可续期"),
                account_info=_account_info(claims, phone, uid, access_exp=access_exp),
            )
        if access_exp and access_exp > now:
            # accessToken 本身没过期却被拒 → 确实失效（被吊销 / 环境不匹配）
            return VerifyResult(
                status="invalid",
                message="登录态无效（accessToken 尚未过期，但上游返回 401）",
                account_info=_account_info(claims, phone, uid, access_exp=access_exp),
            )
        return VerifyResult(
            status="invalid",
            message="登录态已失效（上游返回 401，accessToken 与 refreshToken 均已过期或无法解析）",
            account_info=_account_info(claims, phone, uid, access_exp=access_exp),
        )

    # 未知：退回本地 JWT 结论，并如实标注未联网
    log("codebuddy 联网校验未得出结论，退回本地 JWT 校验")
    if access_exp and access_exp > now:
        status, message = "valid", (
            f"本地校验（未联网）：登录态有效，accessToken 有效期至 "
            f"{_fmt_time(access_exp)}（剩余 {_human_delta(access_exp - now)}）"
        )
    elif refresh_exp and refresh_exp > now:
        status, message = "valid", (
            f"本地校验（未联网）：accessToken 已过期，refreshToken 仍有效至 "
            f"{_fmt_time(refresh_exp)}，可在客户端续期"
        )
    elif access_exp or refresh_exp:
        status, message = "invalid", "登录态已过期（本地校验：accessToken 与 refreshToken 均已过期）"
    else:
        status, message = "error", "本地校验（未联网）：无法解析 JWT 到期时间，未能确认登录态"
    return VerifyResult(
        status=status,
        message=message,
        account_info=_account_info(claims, phone, uid, access_exp=access_exp),
    )


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 CodeBuddy 登录态是否有效。保证不抛出异常。"""
    return _verify(data or {})


def validate(data: dict) -> list[str]:
    """表单校验：access_token 与 uid 必填（空列表 = 通过）。"""
    auth = _read(data)
    errors: list[str] = []
    if not auth["access_token"]:
        errors.append("access_token 不能为空（＝登录态里的 auth.accessToken）")
    if not auth["uid"]:
        errors.append("uid 不能为空（＝登录态里的 account.uid）")
    return errors


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（只出现打码后的账号名）。"""
    claims = _decode_jwt_payload(_read(data)["access_token"]) or {}
    masked = _mask_phone(claims.get("preferred_username"))
    return f"{NAME} | {masked}" if masked else NAME


# ── 可选操作：登录态信息（纯本地、只读、无副作用） ──────

def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据里填了 access_token 才提供）。"""
    if not str((data or {}).get("access_token") or "").strip():
        return []
    return [
        {
            "id": "token_info",
            "name": "登录态信息",
            "kind": "list",
            "group": "查看",
            "order": 10,
            "submit": "刷新",
            "columns": [
                {"key": "field", "label": "字段"},
                {"key": "value", "label": "值"},
            ],
            "empty_text": "登录态中没有可展示的信息",
        },
    ]


def _token_info_rows(data: dict) -> list[dict]:
    """把 JWT 里的**非敏感**信息摊平成表格行；任何 token 原文都不出现。"""
    auth = _read(data)
    claims = _decode_jwt_payload(auth["access_token"]) or {}
    refresh_claims = _decode_jwt_payload(auth["refresh_token"]) or {}
    uid = auth["uid"]

    rows: list[dict] = []

    def add(label: str, value) -> None:
        text = str(value if value is not None else "").strip()
        if text:
            rows.append({"field": label, "value": text})

    def roles(node) -> str:
        if not isinstance(node, dict):
            return ""
        value = node.get("roles")
        if not isinstance(value, list):
            return ""
        return ", ".join(str(item) for item in value if str(item).strip())

    add("签发者 (iss)", claims.get("iss"))
    add("客户端 (azp)", claims.get("azp"))
    add("令牌类型 (typ)", claims.get("typ"))
    add("登录域 (domain)", auth["domain"])
    add("令牌来源 (token_source)", claims.get("token_source"))
    add("账号 (preferred_username)", _mask_phone(claims.get("preferred_username")))
    add("用户 ID (uid)", _short_id(uid))
    add("主体 (sub)", _short_id(claims.get("sub")))
    add("邮箱 (email)", claims.get("email"))
    add("企业 ID (enterpriseId)", auth["enterprise_id"])
    add("领域角色 (realm_access.roles)", roles(claims.get("realm_access")))
    add("资源角色 (resource_access.account.roles)", roles((claims.get("resource_access") or {}).get("account")
                                                         if isinstance(claims.get("resource_access"), dict) else None))
    add("授权范围 (scope)", claims.get("scope"))
    add("允许来源 (allowed-origins)", claims.get("allowed-origins"))
    add("签发时间 (iat)", _fmt_time(_normalize_epoch(claims.get("iat"))))
    add("认证时间 (auth_time)", _fmt_time(_normalize_epoch(claims.get("auth_time"))))

    now = time.time()
    access_exp = _normalize_epoch(claims.get("exp"))
    if access_exp:
        tail = "（已过期）" if access_exp <= now else f"（剩余 {_human_delta(access_exp - now)}）"
        add("访问令牌过期 (exp)", f"{_fmt_time(access_exp)} {tail}")
    refresh_exp = _normalize_epoch(refresh_claims.get("exp"))
    if refresh_exp:
        tail = "（已过期）" if refresh_exp <= now else f"（剩余 {_human_delta(refresh_exp - now)}）"
        add("刷新令牌过期", f"{_fmt_time(refresh_exp)} {tail}")
    return rows


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：登录态信息（纯本地解析，不联网）。"""
    if action_id != "token_info":
        return {"ok": False, "error": "未知操作"}
    if not _read(data)["access_token"]:
        return {"ok": False, "error": "缺少 access_token（＝登录态里的 auth.accessToken）"}
    rows = _token_info_rows(data)
    return {"ok": True, "rows": rows, "total": len(rows)}
