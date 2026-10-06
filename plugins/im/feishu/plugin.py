"""飞书应用凭证验证插件（plugins/im/ 类别下的独立子插件）。

端点：POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
自建/商店应用用 app_id + app_secret 换取 tenant_access_token；
成功时返回 token，失败时 HTTP 200 + code != 0
（常见错误码：10003 app_id 不存在、10007、99991663 app_id/secret 不匹配）。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``app_info``    账号与权限：应用名称 / ID / 状态 / 类型 / 已开通权限
- ``bot_info``    机器人信息：名称 / open_id / 头像链接 / 激活状态
- ``user_detail`` 通讯录用户查询（按 open_id 或邮箱，表单）
- ``list_chats``  群组列表：群名 / chat_id / 成员数 / 类型
- ``list_wikis``  知识库空间：空间名 / space_id / 描述 / 可见性
- ``list_docs``   云文档：文档名 / 类型 / token / 更新时间

并实现 ``capabilities()``：逐个只读探测上述接口权限，决定哪些操作对当前应用可见；
**探测拿不准时抛异常**（框架按「未知 → 全部可用」处理，fail-open），
绝不返回空列表把功能藏掉。token 按 app_id 模块级缓存，快过期时自动刷新。

参考文档：https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
"""
from __future__ import annotations

import datetime
import time

import httpx

from im_common import (
    VerifyResult,
    body_snippet,
    first_list,
    guarded,
    log,
    log_debug,
    make_client,
    safe_json,
)

SERVICE = "feishu"
NAME = "飞书应用"
REQUIRED = (("app_id", "App ID"), ("app_secret", "App Secret"))
DISPLAY_FIELD = "app_id"

BASE = "https://open.feishu.cn"
TOKEN_URL = f"{BASE}/open-apis/auth/v3/tenant_access_token/internal"

# capabilities 里 6 个探测接口串行执行，单请求超时压到 8 秒，
# 否则最坏情况要等 6 × VAULT_TIMEOUT（默认 20 秒 × 6）才返回。
_PROBE_TIMEOUT = 8.0

# tenant_access_token 通常 7200 秒有效；提前 120 秒刷新，避免踩到边界。
_TOKEN_SAFETY = 120
_DEFAULT_EXPIRE = 7200.0

# app_id → (token, 到期时间戳)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


# ── 通用小工具 ───────────────────────────────────────────

def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _error_detail(payload) -> str:
    """尽力从错误响应中提取可读原因（飞书/钉钉/QQ 均有 message 类字段）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "errmsg", "description", "error_description"):
        value = payload.get(key)
        if value:
            return str(value)
    code = payload.get("code")
    if code is None:
        code = payload.get("errcode")
    return f"错误码 {code}" if code is not None else ""


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _text(value) -> str:
    """标量字段 → 展示文本；None / 空串统一给 '—'。"""
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


def _join(value) -> str:
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(parts) if parts else "—"
    return _text(value)


def _describe(value, mapping: dict) -> str:
    """枚举值 → 「中文 (原始值)」。

    原始值一并展示是刻意的：映射表来自公开文档，拿不准时用户仍能看到后端真实返回，
    避免展示一个可能标错的中文标签。
    """
    if value is None or value == "":
        return "—"
    key = value
    try:
        key = int(value)
    except (TypeError, ValueError):
        pass
    label = mapping.get(key)
    return f"{label} ({value})" if label else str(value)


def _fmt_time(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        stamp = float(text)
    except ValueError:
        return text
    try:
        return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return text


def _fail(payload, resp) -> dict:
    """统一的失败结果：飞书的错误一律是 HTTP 200/400 + body 里的 code/msg。"""
    if isinstance(payload, dict):
        code = payload.get("code")
        if code is not None:
            detail = _error_detail(payload) or "未知错误"
            return {"ok": False, "error": f"飞书接口返回错误（{code}: {detail}）"}
    return {"ok": False, "error": f"飞书接口请求失败（HTTP {resp.status_code}）"}


def _data_list(payload, *keys) -> list:
    """飞书列表接口的信封统一是 ``{"code":0,"data":{"items"|"files":[...]}}``。"""
    wanted = keys or ("items", "files", "user_list")
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            for key in wanted:
                value = inner.get(key)
                if isinstance(value, list):
                    return value
    return first_list(payload, *wanted)


def _data_obj(payload, *path):
    """按路径取 ``data`` 下的对象，例如 ``_data_obj(payload, "app")``。"""
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _code_ok(payload) -> bool:
    return isinstance(payload, dict) and payload.get("code") in (0, "0")


# ── tenant_access_token（模块级缓存） ────────────────────

def _store_token(app_id: str, token: str, expire_raw) -> None:
    try:
        ttl = float(expire_raw)
    except (TypeError, ValueError):
        ttl = _DEFAULT_EXPIRE
    if ttl <= 0:
        ttl = _DEFAULT_EXPIRE
    _TOKEN_CACHE[app_id] = (str(token), time.time() + ttl)


def _tenant_token(data: dict) -> tuple[str | None, str | None]:
    """返回 ``(token, error)``。按 ``app_id`` 缓存，快过期时自动刷新。

    每个操作都重新换一次 token 既慢又容易撞飞书的频率限制，所以这里做模块级缓存：
    验证通过后 token 就留在这里，后续操作直接复用。失败时给出可读原因。
    """
    app_id = str((data or {}).get("app_id") or "").strip()
    app_secret = str((data or {}).get("app_secret") or "").strip()
    if not app_id or not app_secret:
        return None, "缺少 App ID 或 App Secret"

    now = time.time()
    cached = _TOKEN_CACHE.get(app_id)
    if cached and cached[1] - _TOKEN_SAFETY > now:
        return cached[0], None

    try:
        with make_client() as client:
            resp = client.post(
                TOKEN_URL,
                headers={"Content-Type": "application/json"},
                json={"app_id": app_id, "app_secret": app_secret},
            )
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        return None, f"获取 tenant_access_token 失败（{type(exc).__name__}）"

    payload = safe_json(resp)
    if isinstance(payload, dict):
        code = payload.get("code")
        token = payload.get("tenant_access_token")
        if code in (0, "0") and token:
            _store_token(app_id, token, payload.get("expire"))
            return str(token), None
        detail = _error_detail(payload) or f"HTTP {resp.status_code}"
        return None, f"获取 tenant_access_token 失败：{detail}"
    return None, f"获取 tenant_access_token 失败（HTTP {resp.status_code}）"


# ── 权限探测 ─────────────────────────────────────────────

# (action_id, 中文名, 路径模板, 查询参数)
_PROBES: tuple[tuple[str, str, str, dict | None], ...] = (
    ("app_info", "应用信息", "/open-apis/application/v6/applications/{app_id}", {"lang": "zh_cn"}),
    ("bot_info", "机器人信息", "/open-apis/bot/v3/info", None),
    ("user_detail", "通讯录用户", "/open-apis/contact/v3/users", {"page_size": 1}),
    ("list_chats", "群组", "/open-apis/im/v1/chats", {"page_size": 1}),
    ("list_wikis", "知识库", "/open-apis/wiki/v2/spaces", {"page_size": 1}),
    ("list_docs", "云文档", "/open-apis/drive/v1/files", {"page_size": 1}),
)
_LABELS = {action_id: label for action_id, label, _, _ in _PROBES}

# tenant_access_token 被拒绝 / 无效时的错误码——出现这些说明是探测环境本身的问题
_AUTH_FAIL_CODES = {99991663, 99991661, 99991664}


def _probe_permissions(client: httpx.Client, token: str, data: dict):
    """逐个只读探测接口权限，互不影响。

    返回 ``(passed, failures, unsure)``：

    - ``passed``   : 通过的 action_id 列表
    - ``failures`` : ``[(中文名, code, msg)]``，接口明确回了错误码
    - ``unsure``   : ``[(中文名, 原因)]``，探测**本身**没拿到结论（网络异常等）
    """
    app_id = str((data or {}).get("app_id") or "").strip()
    passed: list[str] = []
    failures: list[tuple[str, object, str]] = []
    unsure: list[tuple[str, str]] = []
    for action_id, label, path, params in _PROBES:
        if "{app_id}" in path:
            if not app_id:
                continue  # 缺 app_id 是配置问题，不计入权限结论
            path = path.replace("{app_id}", app_id)
        try:
            resp = client.get(
                f"{BASE}{path}",
                headers=_auth(token),
                params=params,
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 单个探测失败不影响其它
            unsure.append((label, type(exc).__name__))
            continue
        payload = safe_json(resp)
        if not isinstance(payload, dict):
            unsure.append((label, f"HTTP {resp.status_code} 响应无法解析"))
            continue
        if _code_ok(payload):
            passed.append(action_id)
        else:
            failures.append((label, payload.get("code"), _error_detail(payload)))
    return passed, failures, unsure


def _failure_note(failures) -> str:
    return "、".join(f"{label}({code}: {_short(msg, 40)})" for label, code, msg in failures)


def capabilities(data) -> dict:
    """探测当前应用真正可用的操作（前端据此隐藏没权限的操作）。

    **fail-open**：只要探测没拿到确切结论（网络抖动、token 被拒），就抛异常——
    框架会当成「未知 → 全部可用」。返回空列表只用于「确实一个权限都没有」。
    """
    data = data or {}
    token, error = _tenant_token(data)
    if not token:
        raise RuntimeError(error or "无法获取 tenant_access_token")

    with make_client() as client:
        passed, failures, unsure = _probe_permissions(client, token, data)

    if unsure:
        raise RuntimeError("权限探测未取得结论：" + "、".join(f"{l}({r})" for l, r in unsure))
    if not passed and failures and all(code in _AUTH_FAIL_CODES for _, code, _ in failures):
        raise RuntimeError("权限探测异常：tenant_access_token 被拒绝")

    # 缺哪些权限只记 DEBUG 日志，不往界面上抛：用户要的是「能用什么」，不是一张权限清单。
    if failures:
        log_debug(f"飞书权限探测：不可用 {len(failures)} 项 → {_failure_note(failures)}")
    if not passed:
        return {"available": [], "note": "当前应用未授权任何可用操作"}
    return {"available": passed, "note": ""}


# ── 操作声明 ─────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]
_USER_COLUMNS = [
    {"key": "name", "label": "名称"},
    {"key": "email", "label": "邮箱", "mono": True},
    {"key": "department", "label": "部门"},
    {"key": "employee_no", "label": "工号"},
    {"key": "status", "label": "状态", "type": "tag"},
]
_CHAT_COLUMNS = [
    {"key": "name", "label": "群名"},
    {"key": "chat_id", "label": "Chat ID", "mono": True},
    {"key": "member_count", "label": "成员数", "type": "number"},
    {"key": "chat_type", "label": "类型", "type": "tag"},
]
_WIKI_COLUMNS = [
    {"key": "name", "label": "空间名"},
    {"key": "space_id", "label": "Space ID", "mono": True},
    {"key": "description", "label": "描述"},
    {"key": "visibility", "label": "可见性", "type": "tag"},
]
_DOC_COLUMNS = [
    {"key": "name", "label": "文档名"},
    {"key": "type", "label": "类型", "type": "tag"},
    {"key": "token", "label": "Token", "mono": True},
    {"key": "modified_time", "label": "更新时间"},
]

_APP_STATUS = {0: "停用", 1: "启用", 2: "未启用", 3: "未启用（商店应用）"}
_APP_SCENE = {0: "自建应用", 1: "商店应用"}
_BOT_STATUS = {
    0: "初始化，租户待安装",
    1: "租户停用",
    2: "租户启用",
    3: "安装后待启用",
    4: "升级待启用",
    5: "license 过期停用",
    6: "套餐到期或降级停用",
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（只读，不设 write）。"""
    return [
        {
            "id": "app_info",
            "name": "账号与权限",
            "kind": "list",
            "group": "账号",
            "order": 10,
            "description": "应用名称 / ID / 状态 / 类型 / 已开通权限",
            "columns": _KV_COLUMNS,
            "empty_text": "未获取到应用信息",
        },
        {
            "id": "bot_info",
            "name": "机器人信息",
            "kind": "list",
            "group": "账号",
            "order": 20,
            "description": "名称 / open_id / 头像链接 / 激活状态",
            "columns": _KV_COLUMNS,
            "empty_text": "该应用未开启机器人能力",
        },
        {
            "id": "user_detail",
            "name": "查询通讯录用户",
            "kind": "form",
            "group": "通讯录",
            "order": 30,
            "description": "按 Open ID 或邮箱查询用户（二选一）",
            "fields": [
                {
                    "key": "open_id",
                    "label": "Open ID",
                    "type": "text",
                    "required": False,
                    "placeholder": "ou_...（与邮箱二选一）",
                },
                {
                    "key": "email",
                    "label": "邮箱",
                    "type": "text",
                    "required": False,
                    "placeholder": "user@example.com（与 Open ID 二选一）",
                },
            ],
            "submit": "查询",
        },
        {
            "id": "list_chats",
            "name": "群组列表",
            "kind": "list",
            "group": "群组",
            "order": 40,
            "description": "应用所在的群组",
            "columns": _CHAT_COLUMNS,
            "empty_text": "未查询到群组",
        },
        {
            "id": "list_wikis",
            "name": "知识库",
            "kind": "list",
            "group": "云文档",
            "order": 50,
            "description": "知识库空间列表",
            "columns": _WIKI_COLUMNS,
            "empty_text": "未查询到知识库",
        },
        {
            "id": "list_docs",
            "name": "云文档",
            "kind": "list",
            "group": "云文档",
            "order": 60,
            "description": "云空间文档列表",
            "columns": _DOC_COLUMNS,
            "empty_text": "未查询到云文档",
        },
    ]


# ── 操作实现 ─────────────────────────────────────────────

def _action_app_info(client, token, params, data) -> dict:
    app_id = str((data or {}).get("app_id") or "").strip()
    if not app_id:
        return {"ok": False, "error": "缺少 App ID"}
    resp = client.get(
        f"{BASE}/open-apis/application/v6/applications/{app_id}",
        headers=_auth(token),
        params={"lang": "zh_cn"},
    )
    payload = safe_json(resp)
    app = _data_obj(payload, "data", "app")
    if not isinstance(app, dict):
        return _fail(payload, resp)

    rows = [
        {"item": "应用名称", "value": _text(app.get("app_name"))},
        {"item": "应用 ID", "value": _text(app.get("app_id")) or app_id},
        {"item": "状态", "value": _describe(app.get("status"), _APP_STATUS)},
        {"item": "类型", "value": _describe(app.get("app_scene_type"), _APP_SCENE)},
    ]
    rows.append({"item": "已开通权限", "value": _scopes_text(client, token, data, app)})
    return {"ok": True, "rows": rows, "columns": _KV_COLUMNS, "empty_text": "未获取到应用信息"}


def _scopes_text(client, token, data, app: dict) -> str:
    """优先用接口返回的 scope 清单；接口没给就退回「探测到的能力」。"""
    raw = app.get("scopes")
    if raw is None:
        raw = app.get("scope")
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("scope") or item.get("scope_name") or item.get("name")
            else:
                name = item
            if name:
                names.append(str(name).strip())
    if names:
        return "、".join(names)

    # 接口未返回 scope：用同一套只读探测反推可用能力，并如实标注来源
    passed, failures, unsure = _probe_permissions(client, token, data)
    parts = ["（接口未返回 scope 清单，以下为接口探测结果）"]
    parts.append("可用：" + ("、".join(_LABELS[i] for i in passed) if passed else "无"))
    if failures:
        parts.append("不可用：" + _failure_note(failures))
    if unsure:
        parts.append("未知：" + "、".join(f"{l}({r})" for l, r in unsure))
    return "；".join(parts)


def _action_bot_info(client, token, params, data) -> dict:
    resp = client.get(f"{BASE}/open-apis/bot/v3/info", headers=_auth(token))
    payload = safe_json(resp)
    bot = payload.get("bot") if isinstance(payload, dict) else None
    if not isinstance(bot, dict):
        bot = _data_obj(payload, "data", "bot")
    if not isinstance(bot, dict):
        return _fail(payload, resp)

    rows = [
        {"item": "名称", "value": _text(bot.get("app_name"))},
        {"item": "Open ID", "value": _text(bot.get("open_id"))},
        {"item": "头像链接", "value": _text(bot.get("avatar_url"))},
        {"item": "激活状态", "value": _describe(bot.get("activate_status"), _BOT_STATUS)},
    ]
    return {"ok": True, "rows": rows, "columns": _KV_COLUMNS, "empty_text": "该应用未开启机器人能力"}


def _user_status(status) -> str:
    if isinstance(status, dict):
        if status.get("is_frozen"):
            return "已冻结"
        if status.get("is_activated"):
            return "已激活"
        return "未激活"
    return "—"


def _action_user_detail(client, token, params, data) -> dict:
    open_id = str((params or {}).get("open_id") or "").strip()
    email = str((params or {}).get("email") or "").strip()
    if not open_id and not email:
        return {"ok": False, "error": "请填写 Open ID 或邮箱（二选一）"}

    if not open_id:
        open_id = _resolve_open_id(client, token, email)
        if not open_id:
            return {"ok": False, "error": f"未找到邮箱 {email} 对应的用户（可能不在通讯录可见范围）"}

    resp = client.get(
        f"{BASE}/open-apis/contact/v3/users/{open_id}",
        headers=_auth(token),
        params={"user_id_type": "open_id"},
    )
    payload = safe_json(resp)
    user = _data_obj(payload, "data", "user")
    if not isinstance(user, dict):
        return _fail(payload, resp)

    row = {
        "name": _text(user.get("name")) if user.get("name") else _text(user.get("en_name")),
        "email": _text(user.get("email")) if user.get("email") else _text(user.get("enterprise_email")),
        "department": _join(user.get("department_ids")),
        "employee_no": _text(user.get("employee_no")),
        "status": _user_status(user.get("status")),
    }
    lines = [f"Open ID: {open_id}"]
    for label, key in (
        ("名称", "name"), ("邮箱", "email"), ("部门", "department"),
        ("工号", "employee_no"), ("状态", "status"),
    ):
        lines.append(f"{label}: {row[key]}")
    return {
        "ok": True,
        "rows": [row],
        "columns": _USER_COLUMNS,
        "empty_text": "未查询到该用户",
        # 表单提交路径只渲染 output，同时给 rows 以便列表视图复用
        "output": "\n".join(lines),
        "total": 1,
    }


def _resolve_open_id(client, token, email: str) -> str:
    """邮箱 → open_id：POST /contact/v3/users/batch_get_id。"""
    resp = client.post(
        f"{BASE}/open-apis/contact/v3/users/batch_get_id",
        headers=_auth(token),
        params={"user_id_type": "open_id"},
        json={"emails": [email]},
    )
    payload = safe_json(resp)
    if not _code_ok(payload):
        return ""
    for item in _data_list(payload, "user_list"):
        if isinstance(item, dict):
            found = str(item.get("user_id") or item.get("open_id") or "").strip()
            if found:
                return found
    return ""


def _action_list_chats(client, token, params, data) -> dict:
    resp = client.get(
        f"{BASE}/open-apis/im/v1/chats", headers=_auth(token), params={"page_size": 50}
    )
    payload = safe_json(resp)
    if not _code_ok(payload):
        return _fail(payload, resp)
    rows = []
    for item in _data_list(payload, "items"):
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": _text(item.get("name")) if item.get("name") else "（未命名）",
            "chat_id": _text(item.get("chat_id")),
            "member_count": item.get("member_count"),
            # 群组接口未必有 chat_type，退回 chat_mode（group / topic / p2p）
            "chat_type": _text(item.get("chat_type")) if item.get("chat_type")
            else _text(item.get("chat_mode")),
        })
    return {
        "ok": True,
        "rows": rows,
        "columns": _CHAT_COLUMNS,
        "empty_text": "未查询到群组",
        "total": len(rows),
    }


def _action_list_wikis(client, token, params, data) -> dict:
    resp = client.get(
        f"{BASE}/open-apis/wiki/v2/spaces", headers=_auth(token), params={"page_size": 50}
    )
    payload = safe_json(resp)
    if not _code_ok(payload):
        return _fail(payload, resp)
    rows = []
    for item in _data_list(payload, "items"):
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": _text(item.get("name")),
            "space_id": _text(item.get("space_id")),
            "description": _text(item.get("description")),
            "visibility": _text(item.get("visibility")),
        })
    return {
        "ok": True,
        "rows": rows,
        "columns": _WIKI_COLUMNS,
        "empty_text": "未查询到知识库",
        "total": len(rows),
    }


def _action_list_docs(client, token, params, data) -> dict:
    resp = client.get(
        f"{BASE}/open-apis/drive/v1/files", headers=_auth(token), params={"page_size": 50}
    )
    payload = safe_json(resp)
    if not _code_ok(payload):
        return _fail(payload, resp)
    rows = []
    for item in _data_list(payload, "files"):
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": _text(item.get("name")),
            "type": _text(item.get("type")),
            "token": _text(item.get("token")),
            "modified_time": _fmt_time(item.get("modified_time")),
        })
    return {
        "ok": True,
        "rows": rows,
        "columns": _DOC_COLUMNS,
        "empty_text": "未查询到云文档",
        "total": len(rows),
    }


_HANDLERS = {
    "app_info": _action_app_info,
    "bot_info": _action_bot_info,
    "user_detail": _action_user_detail,
    "list_chats": _action_list_chats,
    "list_wikis": _action_list_wikis,
    "list_docs": _action_list_docs,
}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    ``params`` 是本次操作的输入（表单字段 / 行参数），``data`` 是凭据本身
    （App ID / App Secret，用于换取 token）。
    """
    params = params or {}
    data = data or {}
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return {"ok": False, "error": f"未知操作：{action_id}"}
    try:
        token, error = _tenant_token(data)
        if not token:
            return {"ok": False, "error": error or "无法获取 tenant_access_token"}
        with make_client() as client:
            return handler(client, token, params, data)
    except httpx.TimeoutException:
        return {"ok": False, "error": "请求超时"}
    except httpx.HTTPError as exc:
        log(f"feishu 操作 {action_id} 网络异常: {type(exc).__name__}")
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}）"}
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        log(f"feishu 操作 {action_id} 未预期异常: {type(exc).__name__}")
        return {"ok": False, "error": f"操作异常（{type(exc).__name__}）"}


# ── 验证 ─────────────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    return errors


def _verify(data: dict) -> VerifyResult:
    app_id = (data.get("app_id") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()
    with make_client() as client:
        resp = client.post(
            TOKEN_URL,
            headers={"Content-Type": "application/json"},
            json={"app_id": app_id, "app_secret": app_secret},
        )
    status = resp.status_code
    payload = safe_json(resp)
    if isinstance(payload, dict):
        code = payload.get("code")
        if code in (0, "0"):
            token = payload.get("tenant_access_token")
            if token:
                # 顺手把 token 存进缓存，后续「操作」无需重新换取
                _store_token(app_id, token, payload.get("expire"))
            return VerifyResult(status="valid", message="凭证有效")
        if code is not None:
            detail = _error_detail(payload)
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")
    return _verify(data)


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
