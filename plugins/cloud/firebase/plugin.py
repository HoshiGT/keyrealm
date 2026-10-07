"""Firebase Admin 密钥验证插件（plugins/cloud/ 类别下的独立子插件）。

Firebase 的「服务账号私钥」（控制台导出的 ``*-firebase-adminsdk-*.json``）在协议层
就是一份**普通 Google 服务账号**，所以换 token 这一步与 ``cloud_gcp`` 完全一致，
共用同目录的 ``google_sa`` 模块。

判定同样分两步、全部只读：

1. **换 token**（决定有效性）：换得到 ⇒ 密钥当前有效；Google 回 ``invalid_grant`` 且
   描述为 ``account not found`` / ``Invalid JWT Signature`` ⇒ 服务账号已删或私钥已轮换
   ⇒ 无效（项目被删的 adminsdk 密钥就是这一种，实测回包即 ``Invalid grant: account not found``）。

2. **探 Firebase 项目**（只作补充信息，不参与判定）：带 token 调 Firebase Management
   ``GET /v1beta1/projects/<project_id>``，回 200 就给出项目状态与 RTDB / Storage 实例名。
   ``cloud_gcp`` 探的是 Resource Manager，这里换成了 Firebase 自己的接口，这也是两个插件
   唯一有意义的差别——同一份密钥若只挂在 Firebase 上，只有这一步能看出项目还在不在。

字段按「一行一项」拆平（邮箱 / 私钥 / 密钥 ID / 项目 ID），不接收整段 JSON。

验证之外另提供两个**只读**操作（``kind: "list"``，都不产生远端变更）：

- **获取详细信息**：把上面第一步的身份信息与第二步的项目产物摊成「字段 / 值」两列；
- **应用列表**：列出该项目下已注册的 Web / Android / iOS 应用。

``capabilities`` 只探一次项目接口来裁剪这两个操作；探测「拿不准」（网络异常、看不懂的
状态码）时一律 fail-open，绝不为了省事把操作全隐藏。

仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档：https://firebase.google.com/docs/reference/firebase-management/rest/v1beta1/projects/get
参考文档：https://firebase.google.com/docs/reference/firebase-management/rest/v1beta1/projects.webApps/list
参考文档：https://firebase.google.com/docs/reference/firebase-management/rest/v1beta1/projects.androidApps/list
参考文档：https://firebase.google.com/docs/reference/firebase-management/rest/v1beta1/projects.iosApps/list
参考文档：https://developers.google.com/identity/protocols/oauth2/service-account
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, first_list, guarded, log
from google_sa import (
    GoogleSAError,
    classify_token_error,
    get_json,
    normalize_sa,
    token_error_detail,
    token_for,
)

NAME = "Firebase 管理密钥"
REQUIRED = (("client_email", "服务账号邮箱"), ("private_key", "私钥（PEM）"))
DISPLAY_FIELD = "client_email"

FIREBASE_URL = "https://firebase.googleapis.com/v1beta1/projects/{project_id}"

# 从 Firebase 项目对象里挑出值得写进 account_info 的资源线索
RESOURCE_KEYS = ("realtimeDatabaseInstance", "storageBucket", "hostingSite", "locationId")

# 列 App 的端点：Firebase Management **没有**统一的 /apps 路由——实测
# ``GET /v1beta1/projects/<pid>/apps`` 直接回 404（路由不存在，而非鉴权失败），
# 只有 webApps / androidApps / iosApps 三条各自独立的路由在。所以「应用列表」是
# 三次只读调用合并出来的，平台名也由「调的哪条路由」决定，不依赖回包里的字段。
APP_LIST_ENDPOINTS = (
    ("WEB", "webApps"),
    ("ANDROID", "androidApps"),
    ("IOS", "iosApps"),
)
APPS_URL = "https://firebase.googleapis.com/v1beta1/projects/{project_id}/{collection}"

_PLATFORM_LABELS = {"WEB": "Web", "ANDROID": "Android", "IOS": "iOS"}
# Firebase 项目 state 是英文枚举，摊表时换成人话；认不出的原样展示
_STATE_LABELS = {"ACTIVE": "运行中", "DELETED": "已删除", "STATE_UNSPECIFIED": "未指定"}

_DETAIL_COLUMNS = [
    {"key": "field", "label": "字段", "width": 130},
    {"key": "value", "label": "值", "mono": True},
]

_APP_COLUMNS = [
    {"key": "appId", "label": "App ID", "mono": True},
    {"key": "displayName", "label": "显示名", "width": 160},
    {"key": "platform", "label": "平台", "type": "tag", "width": 100},
]

# 字段 → 中文标签；没登记的一律按原始英文键展示（与前端的渲染规则一致）
_DETAIL_LABELS = {
    "client_email": "服务账号邮箱",
    "private_key_id": "密钥 ID",
    "project_id": "项目 ID",
    "firebase_state": "Firebase 项目状态",
    "realtimeDatabaseInstance": "实时数据库实例",
    "storageBucket": "Storage 桶",
    "hostingSite": "Hosting 站点",
    "locationId": "默认资源位置",
    "token_expires_in": "access_token 有效期（秒）",
}
# 摊表顺序：先身份、后项目资源。定死顺序是因为信息来自两个来源拼起来的字典，
# 直接用 dict 迭代顺序会随「项目探测成功与否」而变，同一把密钥两次打开行序不一样。
_DETAIL_ORDER = (
    "client_email",
    "private_key_id",
    "project_id",
    "firebase_state",
    "realtimeDatabaseInstance",
    "storageBucket",
    "hostingSite",
    "locationId",
    "token_expires_in",
)


def _identity(sa: dict) -> dict:
    """从归一化后的字段里摘出可展示的身份信息（不含私钥）。"""
    info = {}
    for key in ("client_email", "project_id", "private_key_id"):
        value = str(sa.get(key) or "").strip()
        if value:
            info[key] = value
    return info


def _probe_project(sa: dict, token: str) -> tuple[dict, str]:
    """探一次 Firebase 项目，返回 ``(补充信息, 说明后缀)``；失败只降级为说明文本。"""
    project_id = str(sa.get("project_id") or "").strip()
    if not project_id:
        return {}, "（未填 project_id，未探测 Firebase 项目）"
    try:
        status, payload = get_json(FIREBASE_URL.format(project_id=project_id), token)
    except Exception as exc:  # noqa: BLE001 - 补充探测失败不影响有效性结论
        return {}, f"（Firebase 项目探测失败：{type(exc).__name__}）"

    if status == 200:
        info = {}
        state = str(payload.get("state") or "").strip()
        if state:
            info["firebase_state"] = state
        resources = payload.get("resources")
        if isinstance(resources, dict):
            for key in RESOURCE_KEYS:
                value = str(resources.get(key) or "").strip()
                if value:
                    info[key] = value
        suffix = f"，Firebase 项目 {project_id}"
        if state:
            suffix += f"（{state}）"
        return info, suffix
    if status == 403:
        return {}, "（无 Firebase Management 权限，项目信息不可见）"
    if status == 404:
        return {}, f"（Firebase 项目 {project_id} 不存在或已删除，不影响密钥有效性）"
    return {}, f"（Firebase 项目探测未通过：HTTP {status}）"


@guarded
def verify(data) -> VerifyResult:
    try:
        sa = normalize_sa(data)
    except GoogleSAError as exc:
        # 本地就没法把这组字段用起来（缺项 / 邮箱不像 / 私钥解析不了）⇒ 无法认证
        return VerifyResult(status="invalid", message=str(exc))

    info = _identity(sa)
    log(f"校验 Firebase 密钥 {info.get('client_email', '?')}")
    try:
        status, payload, snippet = token_for(sa)
    except GoogleSAError as exc:
        return VerifyResult(status="invalid", message=str(exc))

    if status == 200 and str(payload.get("access_token") or "").strip():
        extra, suffix = _probe_project(sa, payload["access_token"])
        info.update(extra)
        if payload.get("expires_in"):
            info["token_expires_in"] = payload["expires_in"]
        return VerifyResult(
            status="valid",
            message=f"密钥有效：{info.get('client_email', '')}{suffix}",
            account_info=info,
        )

    kind, message = classify_token_error(status, payload, snippet)
    return VerifyResult(status=kind, message=message, account_info=dict(info))


def validate(data) -> list[str]:
    try:
        normalize_sa(data)
    except GoogleSAError as exc:
        return [str(exc)]
    return []


# ── 操作（只读） ────────────────────────────────────────


def _field(data: dict, key: str) -> str:
    """取一个扁平字段的字符串值（与 manifest 的「一行一项」一致）。"""
    return str((data or {}).get(key) or "").strip()


def _clean_note(suffix: str) -> str:
    """把 ``_probe_project`` 的说明后缀去掉首尾标点，好当成普通单元格展示。

    不能用 ``strip("，。（）")``：那会把 ``…（ACTIVE）`` 结尾的右括号一并吃掉。
    """
    note = str(suffix or "").strip().lstrip("，。")
    if note.startswith("（") and note.endswith("）"):
        note = note[1:-1]
    return note or "—"


def actions(data: dict) -> list[dict]:
    """声明可用操作（**不联网**）。

    字段齐了就两个都声明，能不能用交给 ``capabilities`` 按真实项目权限裁剪——
    这里宁可多声明（``list_apps`` 缺 project_id 时会返回明确提示），也不做本地臆测。
    """
    data = data or {}
    if not (_field(data, "client_email") and _field(data, "private_key")):
        return []
    return [
        {
            "id": "details",
            "name": "获取详细信息",
            "kind": "list",
            "group": "查看",
            "order": 10,
            "description": (
                "服务账号身份 + Firebase 项目信息（项目状态 / 实时数据库 / Storage / Hosting），"
                "只读；项目探测失败也会照常列出身份信息"
            ),
            "columns": _DETAIL_COLUMNS,
            "empty_text": "未获取到详细信息",
        },
        {
            "id": "list_apps",
            "name": "应用列表",
            "kind": "list",
            "group": "查看",
            "order": 20,
            "description": (
                "列出该项目下已注册的 Web / Android / iOS 应用（Firebase Management webApps + "
                "androidApps + iosApps 三条只读路由合并）；需要凭据里填了「项目 ID」"
            ),
            "columns": _APP_COLUMNS,
            "empty_text": "该项目下暂无已注册的应用",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作，绝不产生远端变更（没有写类操作，故不接进度 / 取消钩子）。"""
    try:
        sa = normalize_sa(data)
    except GoogleSAError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        status, payload, snippet = token_for(sa)
    except Exception as exc:  # noqa: BLE001 - 签名 / 网络异常一律拍成人话，不冒泡给框架
        return {"ok": False, "error": f"换取 access_token 失败：{type(exc).__name__}"}

    if status != 200 or not str(payload.get("access_token") or "").strip():
        log(f"Firebase 操作换 token 失败：HTTP {status} {token_error_detail(payload, snippet)}")
        return {"ok": False, "error": "换取 access_token 失败，请先在「验证」里确认密钥有效"}

    token = payload["access_token"]
    try:
        if action_id == "details":
            return _run_details(sa, token, payload.get("expires_in"))
        if action_id == "list_apps":
            return _run_list_apps(sa, token)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def _run_details(sa: dict, token: str, expires_in=None) -> dict:
    """身份 + 项目信息摊成「字段 / 值」两列。

    项目探测用的是 ``_probe_project``——它把各种失败都降级成说明文本，所以这个方法
    几乎不会失败：哪怕没填 project_id，也照样能列出身份信息并说明为什么没探项目。
    """
    info = _identity(sa)
    extra, suffix = _probe_project(sa, token)
    info.update(extra)
    if expires_in:
        info["token_expires_in"] = expires_in
    state = str(info.get("firebase_state") or "").strip()
    if state:
        info["firebase_state"] = _STATE_LABELS.get(state, state)

    rows = []
    for key in _DETAIL_ORDER:
        value = info.get(key)
        if value in (None, ""):
            continue
        rows.append({"field": _DETAIL_LABELS.get(key, key), "value": str(value)})
    # 兜底：万一 _identity / _probe_project 以后多回了键，也别悄悄丢掉
    for key, value in info.items():
        if key in _DETAIL_ORDER or value in (None, ""):
            continue
        rows.append({"field": _DETAIL_LABELS.get(key, key), "value": str(value)})
    rows.append({"field": "项目探测", "value": _clean_note(suffix)})

    return {"ok": True, "rows": rows, "columns": _DETAIL_COLUMNS, "total": len(rows), "has_more": False}


def _app_row(item: dict, platform: str) -> dict:
    """把一条 App 资源拍成一行；appId 缺失时从资源名（``projects/P/webApps/ID``）兜底截出。"""
    app_id = str(item.get("appId") or "").strip()
    if not app_id:
        app_id = str(item.get("name") or "").rsplit("/", 1)[-1]
    return {
        "appId": app_id or "—",
        "displayName": str(item.get("displayName") or "").strip() or "—",
        "platform": _PLATFORM_LABELS.get(platform, platform),
    }


def _run_list_apps(sa: dict, token: str) -> dict:
    """列出项目下已注册的 App（三条平台路由各查一次、合并成一张表）。"""
    project_id = str(sa.get("project_id") or "").strip()
    if not project_id:
        # 这一步不联网就能判死，直接给可执行的动作，别让用户猜为什么空表
        return {"ok": False, "error": "请在凭据里填写「项目 ID」后再列应用"}

    rows = []
    denied = 0
    for platform, collection in APP_LIST_ENDPOINTS:
        status, payload = get_json(
            APPS_URL.format(project_id=project_id, collection=collection), token
        )
        if status == 200:
            for item in first_list(payload, "apps") or []:
                if isinstance(item, dict):
                    rows.append(_app_row(item, platform))
            continue
        if status in (401, 403):
            # 权限位是项目级的：一条路由说不给读，另外两条同样不给读，继续试没意义
            denied += 1
            continue
        if status == 404:
            return {"ok": False, "error": f"项目 {project_id} 不存在，或该账号尚未开通 Firebase"}
        return {"ok": False, "error": f"列出应用失败：HTTP {status}"}

    if not rows and denied == len(APP_LIST_ENDPOINTS):
        return {"ok": False, "error": "无 Firebase Management 权限（服务账号缺少读取 App 列表的权限）"}
    return {"ok": True, "rows": rows, "columns": _APP_COLUMNS, "total": len(rows), "has_more": False}


def capabilities(data: dict) -> dict:
    """报告当前密钥**真正能用**的操作（只探一次只读接口，拿不准时抛异常 → fail-open）。

    - 字段本地都拼不成服务账号，或换 token 时 Google 明确回「账号已删 / 签名不符」
      → 任何操作都跑不起来，直接隐藏；
    - 项目接口回 200 → 两个操作都可用；
    - 回 401 / 403 / 404 → 「应用列表」必然失败，只留「详细信息」（它靠身份信息照样能出表）；
    - 其余情况（网络异常、看不懂的状态码）→ **抛异常**，交给框架按「未知 → 全部可用」兜底。
      绝不为了省事返回空列表：那会把一次网络抖动变成「操作凭空消失」。
    """
    try:
        sa = normalize_sa(data)
    except GoogleSAError:
        return {"available": [], "note": "服务账号字段不完整，无法执行操作"}

    project_id = str(sa.get("project_id") or "").strip()
    if not project_id:
        # 没填项目 ID 时「应用列表」必然报错（插件自己也会给出明确提示），
        # 而「详细信息」只靠身份信息就能出表 ⇒ 只放行后者
        return {"available": ["details"], "note": "未填项目 ID，「应用列表」不可用"}

    try:
        status, payload, snippet = token_for(sa)
    except Exception as exc:  # noqa: BLE001 - 探测失败一律 fail-open
        raise RuntimeError(f"换取 access_token 失败（{type(exc).__name__}），无法判断操作可用性") from exc
    if status != 200 or not str(payload.get("access_token") or "").strip():
        kind, message = classify_token_error(status, payload, snippet)
        if kind == "invalid":
            return {"available": [], "note": message}
        raise RuntimeError(f"换取 access_token 未通过（HTTP {status}），无法判断操作可用性")

    try:
        status, _payload = get_json(
            FIREBASE_URL.format(project_id=project_id), payload["access_token"]
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败一律 fail-open
        raise RuntimeError(f"Firebase 项目探测失败（{type(exc).__name__}），无法判断操作可用性") from exc

    if status == 200:
        return {"available": ["details", "list_apps"], "note": "Firebase 项目可访问"}
    if status in (401, 403):
        return {"available": ["details"], "note": "无 Firebase Management 权限，「应用列表」不可用"}
    if status == 404:
        return {"available": ["details"], "note": "Firebase 项目不存在或已删除，「应用列表」不可用"}
    raise RuntimeError(f"Firebase 项目探测返回 HTTP {status}，无法判断操作可用性")


def get_display_summary(data) -> str:
    """凭据列表里的简短展示：只取邮箱，绝不回显私钥。"""
    email = str((data or {}).get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {email}" if email else NAME
