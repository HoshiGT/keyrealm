"""OpenAI 插件（plugins/llm/openai/，自包含实现）。

参考文档：https://platform.openai.com/docs/api-reference

一份凭据支持两种**认证方式**（字段 ``auth_mode``）：

- ``api_key``（默认）：标准 OpenAI API Key，走 ``GET /v1/models`` 探针；
- ``chatgpt``：Codex CLI 的 ChatGPT 登录态——凭据**不是**普通 API Key，判定以**本地解 JWT
  为主、联网 best-effort 增强**（详见 ``_verify_chatgpt``）。

登录态**按字段分开填**，不要求粘贴整份 ``~/.codex/auth.json``（那样既难维护、又把不必要的
字段一起存了）。三个字段一一对应 ``~/.codex/auth.json`` 里 ``tokens`` 下的同名项：

- ``access_token``（必需）：本地判过期 + 联网校验都用它；
- ``account_id``（可选）：联网校验的 ``chatgpt-account-id`` 头，留空由 JWT claim 兜底；
- ``id_token``（可选）：仅在 access_token 缺展示字段时补充；
- ``refresh_token``（可选）：**只读校验路径绝不使用**；填了之后才会多出「刷新 access_token」
  这个**手动**操作，由用户显式点击，用它在过期后换一组新令牌。

代刷令牌会**轮换** refresh_token，属写副作用，因此绝不混进校验路径——只有用户点那个按钮时才做，
且换回来的新值由框架原子写回本凭据（见 backend/api.py 的 ``updated_data`` 约定），
免得库里与本机 Codex CLI 的登录态错位。

安全：任何 ``message`` / ``account_info`` / ``output`` / ``rows`` 都不出现令牌或 API Key 明文，
展示只用 JWT 里的非敏感 claim（邮箱、套餐、账号 id 前 8 位……）。
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe
from llm_protocols import probe_openai_models, chat_completion, fetch_models, chat_completion_stream

NAME = "OpenAI"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
PROTOCOL = "openai_models"

# ChatGPT 登录态模式在凭据列表里的展示前缀（沿用原 Codex 插件的样式）
CHATGPT_NAME = "Codex"
DISPLAY_NAME = "Codex（ChatGPT 登录态）"

# ── ChatGPT 登录态模式用到的常量 ──────────────────────────
# 官方 Codex 客户端读「个人订阅/账号信息」的端点（二进制里的 chatgpt_base_url + "me"）。
# 依据：openai/codex issue #47197 明确列出该 URL；本机 codex-cli 0.157.1 二进制含
# `chatgpt_base_url = "https://chatgpt.com/backend-api/"` 与 `chatgpt-account-id` 头名。
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
# 官方客户端默认 originator——Codex 后端有第一方白名单，别的值会被拒
# （二进制 login/src/auth/default_client.rs:76 旁的默认值）。
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "codex_cli_rs/0.157.1"

# OAuth 换令牌端点与 Codex CLI 的**公开客户端 id**（不是密钥：它以明文出现在用户令牌的
# `aud` 里，官方 CLI 二进制里也是硬编码的）。只有在用户**显式**点「刷新 access_token」时
# 才会打到这个端点——换令牌会轮换 refresh_token，属写副作用，绝不能混进只读校验路径。
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_SCOPE = "openid profile email"

# id_token / access_token 里自定义 claim 的命名空间
AUTH_CLAIM_NS = "https://api.openai.com/auth"

# 套餐名的可读化（未收录的原样展示）
PLAN_LABELS = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "self_serve_business_prolite": "Business Lite",
    "self_serve_business_usage_based": "Business（用量计费）",
    "enterprise": "Enterprise",
    "enterprise_cbp_automation": "Enterprise（自动化）",
    "enterprise_cbp_usage_based": "Enterprise（用量计费）",
    "edu": "Edu",
    "edu_pro": "Edu Pro",
}

# 余额拿不到时的可选项（「设置」页开关，默认关闭）：发一次最小对话请求判断是否欠费。
# 仅 API Key 模式适用——ChatGPT 登录态走的是订阅额度，不经 api.openai.com 计费。
_api_key_quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")


def _is_chatgpt(data) -> bool:
    """认证方式是否为「ChatGPT 登录态」（``chatgpt``）。只认显式值，缺省一律按 API Key 处理。

    容忍大小写与「chatgpt 登录态…」这类带说明的写法，免得界面/旧数据里的值略有出入就静默走错分支。
    """
    return str((data or {}).get("auth_mode") or "").strip().lower().startswith("chatgpt")


def quota_probe(data: dict):
    """欠费探测：仅 API Key 模式生效（ChatGPT 登录态不经 api.openai.com 计费）。"""
    if _is_chatgpt(data):
        return None
    return _api_key_quota_probe(data or {})


# ── 校验 ──────────────────────────────────────────────────

def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。按认证方式只校验对应字段。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if _is_chatgpt(data):
        if not (data.get("access_token") or "").strip():
            errors.append(
                "认证方式选「ChatGPT 登录态（Codex）」时必须填 access_token"
                "（＝ ~/.codex/auth.json 里 tokens.access_token 的值）"
            )
    elif not (data.get("api_key") or "").strip():
        errors.append("认证方式选「API Key」时必须填写 API Key")
    return errors


# ── 验证（按 auth_mode 分派） ─────────────────────────────

def _verify_api_key_mode(data: dict) -> VerifyResult:
    """auth_mode=api_key：走标准 OpenAI /v1/models 探针。"""
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return probe_openai_models(base_url, api_key)


def _verify_chatgpt_mode(data: dict) -> VerifyResult:
    """auth_mode=chatgpt：验证 Codex 登录态（本地为主，联网增强）。"""
    auth = _read_auth(data)
    if not auth["access_token"]:
        return VerifyResult(
            status="invalid",
            message="缺少 access_token（认证方式选「ChatGPT 登录态（Codex）」时必须填它）",
        )
    return _verify_chatgpt(auth)


def _verify(data: dict) -> VerifyResult:
    data = data or {}
    if _is_chatgpt(data):
        return _verify_chatgpt_mode(data)
    return _verify_api_key_mode(data)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证凭据（API Key 或 ChatGPT 登录态）。保证不抛出异常。"""
    return _verify(data or {})


# ── JWT（本地解码，不验签） ──────────────────────────────

def _b64url_decode(segment: str) -> bytes:
    """base64url 解码。**容忍缺失/多余 padding**（JWT 常省略 ``=``，也可能带 ``=``）。"""
    text = str(segment).strip().replace("-", "+").replace("_", "/").rstrip("=")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text)


def _decode_jwt_payload(token) -> Optional[dict]:
    """解出 JWT 的 payload（dict）；任何异常/非 JWT 一律返回 None。

    刻意**不验签**：手上没有公钥，而且这里只用于读非敏感 claim 与过期时间。
    """
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:  # noqa: BLE001 - 非 JWT / 非法 base64 / 非 JSON 都算解析失败
        return None
    return payload if isinstance(payload, dict) else None


def _extract_claims(payload: Optional[dict]) -> dict:
    """从 JWT payload 里挑出我们关心的（全部非敏感）claim。"""
    payload = payload if isinstance(payload, dict) else {}
    auth = payload.get(AUTH_CLAIM_NS)
    auth = auth if isinstance(auth, dict) else {}
    return {
        "iss": payload.get("iss"),
        "aud": payload.get("aud"),
        "sub": payload.get("sub"),
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
        "email": payload.get("email"),
        "name": payload.get("name"),
        "account_id": auth.get("chatgpt_account_id"),
        "user_id": auth.get("chatgpt_user_id"),
        "plan": auth.get("chatgpt_plan_type"),
        "subscription_start": auth.get("chatgpt_subscription_active_start"),
        "subscription_until": auth.get("chatgpt_subscription_active_until"),
        "organizations": auth.get("organizations"),
        "groups": auth.get("groups"),
        "scopes": payload.get("scp"),
    }


# ── 小工具 ────────────────────────────────────────────────

def _fmt_ts(value) -> str:
    """unix 秒 → 可读 UTC 时间串；非法值返回空串。"""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return ""


def _humanize(seconds) -> str:
    """秒数 → 「2 天 3 小时」这类粗粒度文本。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    if total >= 86400:
        return f"{total // 86400} 天 {total % 86400 // 3600} 小时"
    if total >= 3600:
        return f"{total // 3600} 小时 {total % 3600 // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


def _plan_label(raw) -> str:
    """套餐原始值 → 展示值（如 plus → Plus）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    return PLAN_LABELS.get(text.lower(), text)


def _short_id(value, keep: int = 8) -> str:
    """账号 id 只露前若干位（uuid 本身不算密钥，但没必要整条展示）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return f"{text[:keep]}…" if len(text) > keep else text


def _read_auth(data: dict) -> dict:
    """把用户**分字段**填的登录态摊平成内部结构（不做任何判定）。

    不再要求粘贴整份 auth.json：字段一一对应 `~/.codex/auth.json` 里 `tokens` 下的同名项，
    - ``access_token``：必需（JWT，用于本地判过期 + 联网校验）
    - ``account_id``：可选（联网校验的 ``chatgpt-account-id`` 头；留空由 JWT 里的 claim 兜底）
    - ``id_token``：可选（仅在 access_token 缺展示字段时补充）

    ``refresh_token`` 只在用户**显式**点「刷新 access_token」时用到（那是一次会轮换令牌的
    写操作），只读校验路径绝不碰它。
    """
    def _text(value) -> str:
        return str(value).strip() if value is not None else ""

    return {
        "access_token": _text(data.get("access_token")),
        "id_token": _text(data.get("id_token")),
        "account_id": _text(data.get("account_id")),
        "refresh_token": _text(data.get("refresh_token")),
    }


# ── 联网探针（best-effort，结论只有「有效 / 失效 / 不下结论」三种）──

def _probe_chatgpt_me(access_token: str, account_id: str):
    """``GET /backend-api/me``，返回 ``(verdict, payload, note)``。

    verdict：``"valid"``（200）/ ``"invalid"``（401）/ ``None``（无法下结论，note 说明原因）。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    try:
        # httpx 默认走 HTTP/1.1 —— 正好避开 Cloudflare 对 HTTP/2 指纹的挑战，
        # 所以这里刻意不改 http2。
        with make_client() as client:
            resp = client.get(CHATGPT_ME_URL, headers=headers)
    except httpx.HTTPError as exc:
        return None, None, f"请求未完成（{type(exc).__name__}）"

    code = resp.status_code
    if code == 200:
        payload = safe_json(resp) or {}
        return "valid", payload, ""
    if code == 401:
        return "invalid", None, ""
    if code == 403:
        # 已知：Cloudflare 会对非浏览器客户端返回 403 + Turnstile 挑战页，
        # 官方 codex 自己也吃这个 403，所以这里**绝不能**判失效。
        return None, None, "HTTP 403（很可能是 Cloudflare 人机挑战，不代表令牌失效）"
    if code == 429:
        return None, None, "HTTP 429（触发速率限制）"
    return None, None, f"HTTP {code}"


# ── 展示信息 ──────────────────────────────────────────────

def _account_info(claims: dict, payload: Optional[dict]) -> Optional[dict]:
    """凭据列表「附加信息」列。

    前端 ``extraInfo()`` 只额外展示 2 项（``EXTRA_MAX = 2``）、且只认有中文标签的键，
    所以这里**只给 ``email`` 与 ``plan``**（渲染成「邮箱 x@y.com · 套餐 Plus」）——
    账号 id / 订阅到期 / scopes 等一律放进 ``token_info`` 操作里，免得挤掉这两项。
    """
    payload = payload if isinstance(payload, dict) else {}
    info: dict = {}
    email = claims.get("email") or payload.get("email")
    if email:
        info["email"] = str(email)
    plan = _plan_label(claims.get("plan"))
    if plan:
        info["plan"] = plan
    return info or None


# ── 验证：ChatGPT 登录态 ──────────────────────────────────

def _verify_chatgpt(auth: dict) -> VerifyResult:
    """auth_mode=chatgpt（有 tokens.access_token）的判定路径。"""
    access_claims = _extract_claims(_decode_jwt_payload(auth["access_token"]))
    if not access_claims.get("exp") and not access_claims.get("email"):
        # access_token 不是可解 JWT —— 多半是粘贴时被截断，或粘错了别的内容
        return VerifyResult(
            status="invalid",
            message="access_token 不是可解析的 JWT（内容可能被截断；它是 auth.json 里 tokens.access_token 的完整值）",
        )

    # id_token 只在 access_token 缺字段时做补充展示（两者 claim 基本一致）
    id_claims = _extract_claims(_decode_jwt_payload(auth["id_token"]))
    claims = {k: (v if v not in (None, "", [], {}) else id_claims.get(k)) for k, v in access_claims.items()}

    exp = claims.get("exp")
    try:
        exp_ts = int(float(exp))
    except (TypeError, ValueError):
        exp_ts = None

    account_id = auth["account_id"] or str(claims.get("account_id") or "")
    info = _account_info(claims, None)

    # ① 本地先判过期（过期就没必要再联网了）
    if exp_ts is not None and time.time() >= exp_ts:
        stale = _humanize(time.time() - exp_ts) or "刚刚"
        return VerifyResult(
            status="invalid",
            message=(
                f"本地校验（未联网）：access_token 已于 {_fmt_ts(exp_ts)} 过期，约 {stale}前。"
                "本插件不代刷令牌，请在 Codex 里重新登录后把新的 access_token 更新进来。"
            ),
            account_info=info,
        )

    # ② 联网增强
    verdict, payload, note = _probe_chatgpt_me(auth["access_token"], account_id)
    if verdict == "valid":
        expiry = f"，有效期还剩 {_humanize(exp_ts - time.time())}" if exp_ts else ""
        return VerifyResult(
            status="valid",
            message=f"登录态有效（已联网校验 chatgpt.com）{expiry}",
            account_info=_account_info(claims, payload),
        )
    if verdict == "invalid":
        return VerifyResult(
            status="invalid",
            message="登录态已失效（已联网校验：HTTP 401，access_token 被拒；请在 Codex 里重新登录）",
            account_info=info,
        )

    # ③ 联网不下结论 → 回落本地 exp
    if exp_ts is None:
        return VerifyResult(
            status="error",
            message=f"无法确认登录态（本地校验未联网：JWT 里没有 exp；联网{note or '未完成'}）",
            account_info=info,
        )
    remaining = _humanize(exp_ts - time.time())
    return VerifyResult(
        status="valid",
        message=f"登录态未过期（本地校验未联网：{note or '联网探针未完成'}；JWT 有效期还剩 {remaining}）",
        account_info=info,
    )


# ── 模型列表（仅 API Key 模式） ───────────────────────────

def list_models(data: dict) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。ChatGPT 登录态模式无此操作。"""
    data = data or {}
    if _is_chatgpt(data):
        return []
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip())


# ── 可选操作：简单对话（api_key 模式） ─────────────────────
CHAT_MODEL = "gpt-4o-mini"


# ── 可选操作：登录态详情（chatgpt 模式，纯本地、无副作用、不联网） ──

def _token_info_rows(data: dict) -> list:
    auth = _read_auth(data)
    access_claims = _extract_claims(_decode_jwt_payload(auth["access_token"]))
    id_claims = _extract_claims(_decode_jwt_payload(auth["id_token"]))
    claims = {k: (v if v not in (None, "", [], {}) else id_claims.get(k)) for k, v in access_claims.items()}

    rows: list = []

    def add(field: str, value) -> None:
        text = str(value if value is not None else "").strip()
        if text:
            rows.append({"field": field, "value": text})

    add("模式", "ChatGPT 登录态")
    add("签发者", claims.get("iss"))
    add("签发时间", _fmt_ts(claims.get("iat")))
    exp_ts = claims.get("exp")
    add("过期时间", _fmt_ts(exp_ts))
    try:
        delta = int(float(exp_ts)) - int(time.time())
    except (TypeError, ValueError):
        delta = None
    if delta is not None:
        add("剩余有效期", _humanize(delta) if delta > 0 else f"已过期 {_humanize(-delta)}")
    add("邮箱", claims.get("email"))
    add("账号名称", claims.get("name"))
    add("套餐", _plan_label(claims.get("plan")))
    add("订阅生效", _fmt_ts(claims.get("subscription_start")))
    add("订阅到期", _fmt_ts(claims.get("subscription_until")))
    add("账号 ID", _short_id(auth["account_id"] or claims.get("account_id")))
    add("用户 ID", _short_id(claims.get("user_id"), 12))

    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        parts = []
        for org in orgs[:4]:
            if not isinstance(org, dict):
                continue
            title = org.get("title") or _short_id(org.get("id"))
            role = org.get("role")
            mark = "，默认" if org.get("is_default") else ""
            parts.append(f"{title}{f'（{role}{mark}）' if role else ''}")
        add("组织", " · ".join(parts))

    scopes = claims.get("scopes")
    if isinstance(scopes, list) and scopes:
        add("权限范围", " ".join(str(s) for s in scopes))

    tokens_present = []
    if auth["access_token"]:
        tokens_present.append("access_token")
    if auth["id_token"]:
        tokens_present.append("id_token")
    if tokens_present:
        add("已提供令牌", " · ".join(tokens_present))
    return rows


_CHAT_ACTION = {"id": "chat", "name": "简单对话", "kind": "chat", "model": CHAT_MODEL}

_TOKEN_INFO_ACTION = {
    "id": "token_info",
    "name": "登录态详情",
    "kind": "list",
    "group": "查看",
    "order": 10,
    "submit": "刷新",
    "description": "本地解析 auth.json 与 JWT（不联网、不验签），列出账号与令牌有效期",
    "columns": [
        {"key": "field", "label": "字段", "width": 130},
        {"key": "value", "label": "值", "mono": True},
    ],
    "empty_text": "没有可展示的信息",
}


_REFRESH_ACTION = {
    "id": "refresh",
    "name": "刷新 access_token",
    "kind": "form",
    "group": "维护",
    "order": 20,
    "submit": "刷新",
    "write": True,
    "fields": [],
    # 覆盖默认的「会改动远端数据」文案：这里说清不可逆的副作用
    "confirm": (
        "将用 refresh_token 向 OpenAI 换取一组新的 access_token，并**轮换 refresh_token**。\n"
        "成功后新值会自动写回本凭据；旧的 refresh_token 随即失效——如果你本机也在用 Codex CLI，"
        "它可能需要重新登录。\n\n确认继续？"
    ),
    "description": "用 refresh_token 换一组新令牌（会轮换 refresh_token 并写回本凭据）",
}


def _do_refresh(data: dict) -> dict:
    """用 refresh_token 换一组新令牌，返回 ``{"ok", "output"/"error", "updated_data"}``。

    **只在用户显式点按钮时调用**：换令牌会轮换 refresh_token，必须把新值原子写回本凭据
    （框架负责，见 backend/api.py 的 updated_data 约定），否则用户本机与库里的登录态就错位了。
    """
    refresh_token = (data.get("refresh_token") or "").strip()
    if not refresh_token:
        return {"ok": False, "error": "没有可用的 refresh_token（请先在字段里填入 auth.json 的 tokens.refresh_token）"}

    body = {
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": OAUTH_SCOPE,
    }
    try:
        with make_client() as client:
            resp = client.post(
                OAUTH_TOKEN_URL,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "originator": ORIGINATOR,
                },
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"请求未完成（{type(exc).__name__}）"}

    if resp.status_code != 200:
        detail = ""
        payload = safe_json(resp)
        if isinstance(payload, dict):
            detail = str(payload.get("error_description") or payload.get("error") or "").strip()
        hint = "（refresh_token 已失效或被轮换过，请在 Codex 里重新登录）" \
            if resp.status_code in (400, 401) else ""
        suffix = f"：{detail[:160]}" if detail else ""
        return {"ok": False, "error": f"刷新失败（HTTP {resp.status_code}）{suffix}{hint}"}

    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "刷新响应不是 JSON"}

    new_access = str(payload.get("access_token") or "").strip()
    if not new_access:
        return {"ok": False, "error": "刷新响应里没有 access_token"}

    patch = {"access_token": new_access}
    new_refresh = str(payload.get("refresh_token") or "").strip()
    new_id = str(payload.get("id_token") or "").strip()
    if new_refresh:
        patch["refresh_token"] = new_refresh   # 轮换后的新刷新令牌，必须一并写回
    if new_id:
        patch["id_token"] = new_id

    claims = _extract_claims(_decode_jwt_payload(new_access))
    exp_text = _fmt_ts(claims.get("exp"))
    parts = ["已刷新 access_token"]
    if exp_text:
        parts.append(f"新令牌有效期至 {exp_text}")
    parts.append("refresh_token 已轮换并写回本凭据" if new_refresh else "响应里没有新 refresh_token（沿用原值）")
    return {"ok": True, "output": "；".join(parts), "updated_data": patch}


def actions(data: dict) -> list[dict]:
    """声明可用操作，按认证方式返回：

    - ``api_key``：凭据含 API Key 时提供「简单对话」（流式）；
    - ``chatgpt``：填了 access_token 时提供「登录态详情」（纯本地只读，不联网）。
    """
    data = data or {}
    if _is_chatgpt(data):
        if not (data.get("access_token") or "").strip():
            return []
        acts = [dict(_TOKEN_INFO_ACTION)]
        # 只有填了 refresh_token 才给「刷新」——没它就无从刷新
        if (data.get("refresh_token") or "").strip():
            acts.append(dict(_REFRESH_ACTION))
        return acts
    if not (data.get("api_key") or "").strip():
        return []
    return [dict(_CHAT_ACTION)]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话（api_key）或登录态详情（chatgpt）。"""
    data = data or {}
    params = params or {}

    if _is_chatgpt(data):
        if action_id == "refresh":
            return _do_refresh(data)
        if action_id != "token_info":
            return {"ok": False, "error": "未知操作"}
        try:
            rows = _token_info_rows(data)
        except Exception as exc:  # noqa: BLE001 - 操作边界兜底
            return {"ok": False, "error": f"解析失败：{type(exc).__name__}"}
        return {"ok": True, "rows": rows, "total": len(rows)}

    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        params.get("model") or CHAT_MODEL,
        params.get("messages") or [],
        protocol="openai",
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。

    非 chat 的操作（如 chatgpt 模式的 ``token_info``，只读列表）交回 ``run_action``，
    保证两条路径的产出与 ``actions()`` 声明一致。
    """
    if action_id != "chat":
        return run_action(action_id, params, data)
    data = data or {}
    params = params or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        params.get("model") or CHAT_MODEL,
        params.get("messages") or [],
        protocol="openai",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本（两种模式各自样式，均不出现令牌）。"""
    data = data or {}
    if not _is_chatgpt(data):
        base_url = data.get("base_url") or DEFAULT_BASE_URL
        return f"{NAME} | {base_url}"

    auth = _read_auth(data)
    if not auth["access_token"]:
        return DISPLAY_NAME
    claims = _extract_claims(_decode_jwt_payload(auth["access_token"]))
    plan = _plan_label(claims.get("plan"))
    return f"{CHATGPT_NAME} | ChatGPT 登录态" + (f" · {plan}" if plan else "")
