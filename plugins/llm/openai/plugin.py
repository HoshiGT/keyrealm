"""OpenAI 插件（plugins/llm/openai/，自包含实现）。

参考文档：https://platform.openai.com/docs/api-reference

一份凭据支持两种**认证方式**（字段 ``auth_mode``）：

- ``api_key``（默认）：标准 OpenAI API Key，走 ``GET /v1/models`` 探针；
- ``chatgpt``：Codex CLI 的 ChatGPT 登录态——凭据**不是**普通 API Key，判定以**本地解 JWT
  为主、联网 best-effort 增强**（详见 ``_verify_chatgpt``）。

登录态**按字段分开填**，不要求粘贴整份 ``~/.codex/auth.json``（那样既难维护、又把不必要的
字段一起存了）。字段一一对应 ``~/.codex/auth.json`` 里 ``tokens`` 下的同名项：

- ``access_token``（必需）：本地判过期 + 联网校验都用它；
- ``id_token``（可选）：仅在 access_token 缺展示字段时补充；
- ``refresh_token``（可选）：**只读校验路径绝不使用**；填了之后才会多出「刷新 access_token」
  这个**手动**操作，由用户显式点击，用它在过期后换一组新令牌。

联网时的 ``chatgpt-account-id`` 头不再单列一个输入字段（那个字段与 JWT claim 同源、纯属重复，
已删除）：一律从 access_token 的 JWT claim ``chatgpt_account_id`` 里取（见 ``_account_id_from``），
取不到就**不带**这个头。

另外用到两个**非公开/实验性**的 chatgpt.com 端点（与 ``/backend-api/me`` 同源）：
额度使用情况 ``GET /backend-api/wham/usage``（只读，用于「附加信息」的「使用率」一行）和
Codex 对话 ``POST /backend-api/codex/responses``（SSE 流式，「操作」里的「简单对话」）。
两者 403 大概率是 Cloudflare 人机挑战，一律按「未完成/不可用」处理，**绝不判凭据失效**。

代刷令牌会**轮换** refresh_token，属写副作用，因此绝不混进校验路径——只有用户点那个按钮时才做，
且换回来的新值由框架原子写回本凭据（见 backend/api.py 的 ``updated_data`` 约定），
免得库里与本机 Codex CLI 的登录态错位。

安全：任何 ``message`` / ``account_info`` / ``output`` / ``rows`` 都不出现令牌或 API Key 明文，
展示只用 JWT 里的非敏感 claim（邮箱、套餐、账号 id 前 8 位……）。
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
    timeout,
)
from llm_common import make_quota_probe
from llm_protocols import (probe_openai_models, chat_completion, fetch_models,
                           chat_completion_stream, resolve_chat_model)

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
# 官方 Codex 客户端默认 originator——Codex 后端有第一方白名单，别的值会被拒
# （二进制 login/src/auth/default_client.rs:76 旁的默认值）。
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "codex_cli_rs/0.157.1"

# 另外两个 **非公开/实验性** 的 chatgpt.com 端点（与 /backend-api/me 同源，没有官方文档）：
# 额度使用情况（只读，用于「附加信息」的「使用率」一行）
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Codex 对话（SSE 流式，「操作」里的「简单对话」）
CHATGPT_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

# 只有**付费套餐**才有额度窗（free 没有），因此只在付费套餐下才去读使用率。
_USAGE_PLAN_PREFIXES = ("plus", "pro", "enterprise", "team", "business", "self_serve_business")
# 额度窗口按**时长**辨认（响应里 primary/secondary 的顺序不可信）：18000 秒 = 5 小时窗，
# 它就是界面上「使用率」要用的那个窗口（另一个 604800 秒 = 7 天窗）。
_PRIMARY_WINDOW_SECONDS = 18000
# 读使用率的短超时：它只是给「附加信息」锦上添花，不能拖慢 verify
_USAGE_TIMEOUT = 6.0

# ── codex 对话的模型名 ────────────────────────────────────────────────────
# **这里不写死任何模型名**：清单一律现取——优先官方目录端点（`_codex_models_from_api`），
# 其次 Codex CLI 的本地缓存（`~/.codex/models_cache.json`，`_codex_models_from_cli_cache`）；
# 两者都取不到就返回空，由界面上的「模型」下拉（允许手输）兜底。
#
# ⚠️ **目录端点按客户端版本返回**：`?client_version=` 低于 `0.159.0` 时服务端不返回
# `gpt-6.1-sol`（`0.157.1` → 9 条；`0.159.0` → 10 条且含 `gpt-6.1-sol`），而该参数**必填**
# （不带 → 400）。版本写死会让客户端永久停留在旧目录 → 见 `_codex_client_version`。
#
# 另注：目录清单**不等于**端点的准入白名单——`gpt-5.5` 标 `hide` 仍可对话，
# `gpt-6.1-sol` 不在目录里但 `/codex/responses` 接受。因此「列表里没有」只影响下拉显示，
# 不影响能否调用。


def _version_ge(value: str, floor: str) -> bool:
    """``value`` 是否 >= ``floor``（点分数字比较；解析不了当 False）。"""
    def parts(v: str) -> list:
        try:
            return [int(x) for x in str(v).strip().split(".") if x != ""]
        except ValueError:
            return []
    a, b = parts(value), parts(floor)
    return bool(a) and bool(b) and a >= b


def _codex_client_version() -> str:
    """取目录端点 `?client_version=` 要用的版本号。

    优先用**本机 Codex 的真实版本**（`models_cache.json` 的 `client_version` →
    `version.json` 的 `latest_version`）；但**不低于** `_CODEX_MIN_CLIENT_VERSION`——
    低于它服务端会隐去较新的模型（如 `gpt-6.1-sol`）；本机 codex 版本较旧时会命中这一条。
    该参数**必填**（不带 → 400），所以必须有兜底值。
    """
    detected = ""
    for path, key in ((_CODEX_CLI_CACHE, "client_version"),
                      (os.path.expanduser("~/.codex/version.json"), "latest_version")):
        try:
            with open(path, encoding="utf-8") as f:
                detected = str((json.load(f) or {}).get(key) or "").strip()
        except Exception:  # noqa: BLE001 - 读不到就试下一个来源
            continue
        if detected:
            break
    return detected if _version_ge(detected, _CODEX_MIN_CLIENT_VERSION) else _CODEX_MIN_CLIENT_VERSION

# 官方**目录端点**（Codex CLI 自己走的就是这条）。它不属于公开 API，但与既有的
# `/backend-api/me` 同源、**只读**。路径与查询参数取自官方仓库 `openai/codex` 的
# `codex-rs/codex-api/src/endpoint/models.rs`（`path() == "models"`，并追加
# `?client_version=<ver>`）。响应形如 `{"models":[{"slug":…,"visibility":…}, …]}`。
# 响应为 HTTP 200，形如 9 条（6 条 `visibility=="list"`、3 条 `"hide"`）；
# **不带** `chatgpt-account-id` 头同样 200（认证层与 `/me` 完全相同，无此头也认）。
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
# `?client_version=` 的**下界**：低于它服务端会隐去较新模型（`0.159.0` 起才返回 `gpt-6.1-sol`）。
# 真实版本由 `_codex_client_version()` 现取；只有本机 codex 版本更低/读不到时才退回这个下界。
# 它是**版本号不是模型名**，且该参数必填（不带 → 400），所以必须有兜底。
_CODEX_MIN_CLIENT_VERSION = "0.159.1"
# /codex/responses 的 instructions 必填且不能为空（缺了会被 400）
CODEX_INSTRUCTIONS = "You are Codex, a coding assistant. Answer concisely."

# Codex CLI 自己把「本账号可用模型」缓存在这里（它的 models-manager 从远端拉完后落盘）
_CODEX_CLI_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def _codex_models_from_cli_cache() -> list:
    """从 **Codex CLI 自己的缓存**读该账号可用的模型清单（best-effort、**只读**）。

    Codex CLI 会把「这个账号能用哪些模型」写进 `~/.codex/models_cache.json`
    （结构 `{"fetched_at":…,"client_version":…,"identity":…,"models":[…]}`，由它的
    `models-manager` 从远端拉取）。那是**该账号真实**的清单，比内置的名字准，所以优先用它；
    缓存不在、读不动、格式不认识、或列表为空，都退回内置那份。

    元素形状各版本可能不同（字符串 / 含 `slug`|`id`|`name` 的对象都见过），两种都认。
    """
    try:
        with open(_CODEX_CLI_CACHE, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:  # noqa: BLE001 - 只是锦上添花，读不到就用内置的
        return []
    models = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        return []
    out: list = []
    for item in models:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("slug") or item.get("id") or item.get("name") or ""
        else:
            continue
        name = str(name).strip()
        if name and name not in out:
            out.append(name)
    return out


def _codex_models_from_api(access_token: str, account_id: str = "") -> list:
    """向**官方目录端点**要该账号可用的模型（Codex CLI 自己走的就是这条）。

    端点与查询参数见 `CODEX_MODELS_URL` 的注释（取自官方仓库）。响应形如
    ``{"models": [{"slug": …, "visibility": "list", "supported_in_api": true, …}]}``。

    这是**该账号真实**的清单，比任何内置名单都准，所以放在最前面。
    尽力而为：任何失败都返回 ``[]``，由调用方回落到 CLI 缓存、再回落到内置清单。
    ``visibility`` 只有**存在且不是 "list"** 时才排除——那是官方 CLI 的字段，不是公开契约，
    拿不准时宁可多列（多列无害，少列会让用户以为模型没了）。
    """
    if not access_token:
        return []
    try:
        with make_client() as client:
            resp = client.get(
                CODEX_MODELS_URL,
                params={"client_version": _codex_client_version()},
                headers=_chatgpt_headers(access_token, account_id),
            )
        if resp.status_code != 200:
            return []
        payload = safe_json(resp)
    except Exception:  # noqa: BLE001 - 只是列表，失败就用兜底
        return []
    items = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        visibility = item.get("visibility")
        if visibility is not None and str(visibility) != "list":
            continue
        slug = str(item.get("slug") or "").strip()
        if slug and slug not in out:
            out.append(slug)
    return out

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
# 官方 SDK 明确：`max_tokens` 已 **deprecated**，改用 `max_completion_tokens`
# （原文还特别写了它 "is not compatible with o-series models" —— o 系列**必须**用新名）。
# 注意这只影响 **api_key 模式**的 Chat Completions；codex 登录态走的是 `/codex/responses`，
# 用的是 `max_output_tokens`，与这里无关。
TOKEN_PARAM = "max_completion_tokens"

_api_key_quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai", TOKEN_PARAM)


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
    - ``id_token``：可选（仅在 access_token 缺展示字段时补充）

    以前还有一个可手填的 ``account_id`` 字段（联网头 ``chatgpt-account-id`` 的值），但它与
    access_token 的 JWT claim 同源、纯属重复，已删除——联网头一律改由 claim 兜底
    （见 ``_account_id_from``）。

    ``refresh_token`` 只在用户**显式**点「刷新 access_token」时用到（那是一次会轮换令牌的
    写操作），只读校验路径绝不碰它。
    """
    def _text(value) -> str:
        return str(value).strip() if value is not None else ""

    return {
        "access_token": _text(data.get("access_token")),
        "id_token": _text(data.get("id_token")),
        "refresh_token": _text(data.get("refresh_token")),
    }


def _account_id_from(claims: dict) -> str:
    """联网头 ``chatgpt-account-id`` 的值：只从 access_token 的 JWT claim 里取。

    取不到就返回空串——调用方据此**不带**这个头（该头不是必需的，401 与否看 access_token）。
    """
    return str((claims or {}).get("account_id") or "").strip()


def _chatgpt_headers(access_token: str, account_id: str = "", *, stream: bool = False) -> dict:
    """chatgpt.com 后端接口的公共请求头（``/me``、``usage``、codex 对话三处共用）。

    ``account_id`` 为空时**不带** ``chatgpt-account-id`` 头。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers



# ── 联网探针（best-effort，结论只有「有效 / 失效 / 不下结论」三种）──

def _probe_chatgpt_me(access_token: str, account_id: str):
    """``GET /backend-api/me``，返回 ``(verdict, payload, note)``。

    verdict：``"valid"``（200）/ ``"invalid"``（401）/ ``None``（无法下结论，note 说明原因）。
    """
    headers = _chatgpt_headers(access_token, account_id)
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


# ── 额度使用情况（best-effort 只读；取不到就什么都不加） ──

def _is_paid_plan(raw) -> bool:
    """套餐是否属于「有额度窗」的付费档（``free`` 排除在外）。

    认不出来的一律当免费处理——宁可少读一次，也不要在 free 账号上显示一个空/假的使用率。
    """
    text = str(raw or "").strip().lower()
    return any(text == prefix or text.startswith(prefix) for prefix in _USAGE_PLAN_PREFIXES)


def _pick_usage_window(rate_limit: dict) -> Optional[dict]:
    """从 ``rate_limit`` 里挑出「使用率」要用的那个窗口。

    响应里有 ``primary_window`` / ``secondary_window`` 两个窗口，但**顺序不可信**，
    所以按 ``limit_window_seconds`` 判时长：18000 秒（5 小时）就是要用的那个。
    认不出时长时退回名为 ``primary_window`` 的那个；两个都拿不到则返回 None（不显示）。
    """
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        try:
            if int(window.get("limit_window_seconds")) == _PRIMARY_WINDOW_SECONDS:
                return window
        except (TypeError, ValueError):
            continue
    fallback = rate_limit.get("primary_window")
    return fallback if isinstance(fallback, dict) else None


def _fetch_usage_rate(access_token: str, account_id: str) -> Optional[float]:
    """读「额度使用率」，返回 0.0–1.0 或 ``None``。

    **只读**接口（绝不触碰会轮换令牌的刷新路径）。任何异常——403 人机挑战、网络失败、
    非 JSON、字段缺失——都返回 ``None``：调用方据此**什么都不加**，界面上绝不出现假值，
    也绝不影响 verify 的结论。
    """
    if not access_token:
        return None
    try:
        with make_client(request_timeout=_USAGE_TIMEOUT) as client:
            resp = client.get(CHATGPT_USAGE_URL, headers=_chatgpt_headers(access_token, account_id))
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    window = _pick_usage_window(rate_limit)
    if not window:
        return None
    try:
        percent = float(window.get("used_percent"))
    except (TypeError, ValueError):
        return None
    if percent != percent:  # NaN 防护
        return None
    return max(0.0, min(1.0, percent / 100.0))


# ── 展示信息 ──────────────────────────────────────────────

def _account_info(claims: dict, payload: Optional[dict], usage_rate: Optional[float] = None) -> Optional[dict]:
    """凭据列表「附加信息」列。

    前端 ``extraInfo()`` 只额外展示 2 项（``EXTRA_MAX = 2``）、且只认有中文标签的键，
    所以固定项**只给 ``email`` 与 ``plan``**（渲染成「邮箱 x@y.com · 套餐 Plus」）——
    账号 id / 订阅到期 / scopes 等一律放进 ``token_info`` 操作里，免得挤掉这两项。

    ``usage_rate`` 是例外：前端对它有专门的「使用率 X%」行且**不吃** ``EXTRA_MAX`` 配额，
    所以单独作为参数传入、有值时才追加（``None`` = 没读到，什么都不加）。
    """
    payload = payload if isinstance(payload, dict) else {}
    info: dict = {}
    email = claims.get("email") or payload.get("email")
    if email:
        info["email"] = str(email)
    plan = _plan_label(claims.get("plan"))
    if plan:
        info["plan"] = plan
    if usage_rate is not None:
        info["usage_rate"] = usage_rate
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

    account_id = _account_id_from(claims)
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

    # ② 联网增强：先 best-effort 读一次额度使用率（只读、短超时、失败即忽略）。
    #    只有付费套餐才有额度窗，free 账号不浪费这一次请求。
    usage_rate = None
    if _is_paid_plan(claims.get("plan")):
        usage_rate = _fetch_usage_rate(auth["access_token"], account_id)
    info = _account_info(claims, None, usage_rate)

    verdict, payload, note = _probe_chatgpt_me(auth["access_token"], account_id)
    if verdict == "valid":
        expiry = f"，有效期还剩 {_humanize(exp_ts - time.time())}" if exp_ts else ""
        return VerifyResult(
            status="valid",
            message=f"登录态有效（已联网校验 chatgpt.com）{expiry}",
            account_info=_account_info(claims, payload, usage_rate),
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

def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。

    api_key 模式打 ``/v1/models``；chatgpt 模式的目录端点**按客户端版本**发清单
    （见 `_codex_client_version`），再退到 Codex CLI 的本地缓存。**不再有内置兜底名单**——
    两者都拿不到就返回空，由界面允许手输的「模型」下拉兜底。

    ``force=True`` 是用户点「刷新模型列表」——穿透 24h 模型清单缓存（api_key 模式）。
    """
    data = data or {}
    if _is_chatgpt(data):
        # 两级：官方目录端点（该账号真实清单）→ Codex CLI 的本地缓存；都空就空
        auth = _read_auth(data)
        return (
            _codex_models_from_api(auth.get("access_token", ""), _chatgpt_account_id(data))
            or _codex_models_from_cli_cache()
        )
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip(), force)


# ── 可选操作：简单对话（api_key 模式） ─────────────────────


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
    add("账号 ID", _short_id(claims.get("account_id")))
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


# ── 可选操作：简单对话（chatgpt 模式，走实验性的 codex 端点） ──

def _codex_input(messages: list) -> list:
    """把「简单对话」的 ``{role, content}`` 历史转成 Responses API 的 ``input`` 数组。

    ``input`` **必须是数组**（缺了会被 400）；用户的轮次内容块类型是 ``input_text``、
    助手的是 ``output_text``（该接口对两种角色的块类型有区分）。
    """
    items: list = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip() or "user"
        text = str(msg.get("content") or "")
        if not text:
            continue
        part_type = "output_text" if role == "assistant" else "input_text"
        items.append({
            "type": "message",
            "role": role,
            "content": [{"type": part_type, "text": text}],
        })
    return items


def _sse_error(event: dict) -> str:
    """从 SSE 的失败/错误事件里抽出可读的错误文本。"""
    for source in (event, event.get("response")):
        if not isinstance(source, dict):
            continue
        err = source.get("error")
        if isinstance(err, dict):
            text = str(err.get("message") or err.get("code") or "").strip()
            if text:
                return f"对话失败：{text[:200]}"
    return "对话失败（服务端返回了错误事件）"


def _codex_chat(access_token: str, account_id: str, model: str, messages: list, on_delta=None) -> dict:
    """codex 模式「简单对话」：``POST /backend-api/codex/responses``（SSE 流式）。

    返回 ``{"ok": True, "output": 全文}`` 或 ``{"ok": False, "error": ..., "output": 已收到的部分}``。
    这是个**非公开/实验性**端点：403 大概率是 Cloudflare 人机挑战，按「该操作用不了」处理，
    **不代表凭据失效**（与 ``/backend-api/me`` 探针同一套判断）。
    """
    if not access_token:
        return {"ok": False, "error": "缺少 access_token", "output": ""}
    model = str(model or "").strip()
    if not model:
        return {"ok": False, "error": "缺少模型名", "output": ""}
    body = {
        "model": model,
        "instructions": CODEX_INSTRUCTIONS,  # 必填且不能为空，缺了会被 400
        "input": _codex_input(messages),
        "store": False,
        "stream": True,                      # 恒为 true：本路径只处理 SSE
    }
    headers = _chatgpt_headers(access_token, account_id, stream=True)
    headers["Content-Type"] = "application/json"
    headers["OpenAI-Beta"] = "responses=experimental"

    parts: list = []
    emit = on_delta or (lambda _t: None)
    try:
        with make_client() as client:
            with client.stream("POST", CHATGPT_RESPONSES_URL, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    resp.read()
                    snippet = body_snippet(resp)
                    detail = f"：{snippet}" if snippet else ""
                    note = "（可能是 Cloudflare 人机挑战，不代表凭据失效）" if resp.status_code == 403 else ""
                    return {"ok": False, "error": f"HTTP {resp.status_code}{note}{detail}", "output": ""}
                for line in resp.iter_lines():
                    # SSE 里只认 data: 行（event:/id:/心跳一律忽略）
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            parts.append(delta)
                            emit(delta)
                    elif kind == "response.completed":
                        break
                    elif kind in ("response.failed", "error"):
                        return {"ok": False, "error": _sse_error(event), "output": "".join(parts)}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"请求超时（{int(timeout())} 秒）", "output": "".join(parts)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）", "output": "".join(parts)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常（{type(exc).__name__}）", "output": "".join(parts)}

    output = "".join(parts)
    if not output.strip():
        return {"ok": False, "error": "响应中没有可读内容", "output": ""}
    return {"ok": True, "output": output.strip()}


def _chatgpt_account_id(data: dict) -> str:
    """从凭据里解出 chatgpt-account-id（= access_token 的 JWT claim）。"""
    auth = _read_auth(data)
    return _account_id_from(_extract_claims(_decode_jwt_payload(auth["access_token"])))


_CHAT_ACTION = {"id": "chat", "name": "简单对话", "kind": "chat", "model": ""}

# codex 模式的「简单对话」：走 chatgpt.com 的实验性端点；模型运行时取目录清单首项，可被用户手输覆盖
_CODEX_CHAT_ACTION = {
    "id": "chat",
    "name": "简单对话",
    "kind": "chat",
    "model": "",
    "group": "对话",
    "order": 5,
    "description": "直连 chatgpt.com 的实验性 codex 端点（SSE 流式）；模型名可手输覆盖",
}


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


def _resolve_model(data: dict, params: dict) -> str:
    """解析本次「简单对话」用的模型：用户指定优先，否则取运行时模型清单首项。

    **绝不退回任何写死的模型名**——拿不到就返回空串，由调用方提示用户手选。
    """
    return resolve_chat_model(params.get("model"), lambda: list_models(data))


def actions(data: dict) -> list[dict]:
    """声明可用操作，按认证方式返回：

    - ``api_key``：凭据含 API Key 时提供「简单对话」（流式）；
    - ``chatgpt``：填了 access_token 时提供「简单对话」（codex 端点，流式）与
      「登录态详情」（纯本地只读，不联网）。
    """
    data = data or {}
    if _is_chatgpt(data):
        if not (data.get("access_token") or "").strip():
            return []
        acts = [dict(_TOKEN_INFO_ACTION), dict(_CODEX_CHAT_ACTION)]
        # 只有填了 refresh_token 才给「刷新」——没它就无从刷新
        if (data.get("refresh_token") or "").strip():
            acts.append(dict(_REFRESH_ACTION))
        return acts
    if not (data.get("api_key") or "").strip():
        return []
    return [dict(_CHAT_ACTION)]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话（api_key / codex）或登录态详情（chatgpt）。"""
    data = data or {}
    params = params or {}

    if _is_chatgpt(data):
        if action_id == "refresh":
            return _do_refresh(data)
        if action_id == "chat":
            model = _resolve_model(data, params)
            if not model:
                return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
            auth = _read_auth(data)
            return _codex_chat(
                auth["access_token"],
                _chatgpt_account_id(data),
                model,
                params.get("messages") or [],
            )
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
    model = _resolve_model(data, params)
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
        token_param=TOKEN_PARAM,
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。

    非 chat 的操作（如 chatgpt 模式的 ``token_info``，只读列表）交回 ``run_action``，
    保证两条路径的产出与 ``actions()`` 声明一致。chat 在两种认证方式下都是流式的，
    只是走的端点不同（api_key → ``/chat/completions``，chatgpt → codex 端点）。
    """
    data = data or {}
    params = params or {}
    if action_id != "chat":
        return run_action(action_id, params, data)
    if _is_chatgpt(data):
        model = _resolve_model(data, params)
        if not model:
            return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
        auth = _read_auth(data)
        return _codex_chat(
            auth["access_token"],
            _chatgpt_account_id(data),
            model,
            params.get("messages") or [],
            on_delta=on_delta,
        )
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    model = _resolve_model(data, params)
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
        token_param=TOKEN_PARAM,
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
