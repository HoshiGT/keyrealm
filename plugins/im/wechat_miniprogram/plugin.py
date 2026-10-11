"""微信小程序 AppID + AppSecret 验证插件。

验证走 ``POST /cgi-bin/stable_token``，请求体 ``force_refresh=false``：

- 若该小程序当前已有一个仍在有效期内的 access_token，微信会**原样返回同一个 token** 并给出
  剩余 ``expires_in``，**不会作废任何既有凭据**；
- 只有在当前没有可用 token 时才会新签发一个——这是任何取 token 行为都有的正常结果。

因此本插件的验证代价极低；``force_refresh=true`` **绝不可用**：它每天限 20 次、两次间隔需
≥30 秒，且会**立即作废**上一个 token（可能打断其他正在使用该小程序的服务）。

错误码到「有效 / 受限 / 无效」的映射遵循框架统一口径：**IP 白名单、配额、频率、未授权属
「有效但受限」**；只有 ``AppID / AppSecret 本身错误``才判 invalid。

本插件同时声明 ``batch_verify: false``：批量验证会对同一小程序并发取 token，而 token 是
账号级单例资源，并发签发既无意义也可能相互影响，故交由用户手工逐条验证。

``access_token`` 出现在 query string 中（微信的固定设计），故异常路径只回显错误码的中文
映射；未知错误码回显 ``errmsg`` 前会先把 AppSecret 从文本中抹掉。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``token``      获取接口调用凭据：确认 AppSecret 可用，并查看 token 剩余有效期
- ``basicinfo``  小程序基础信息：名称 / 头像 / 主体 / 认证状态
- ``quota``      接口调用配额：查询指定接口的日限额 / 已用量 / 剩余量

参考文档：https://developers.weixin.qq.com/doc/service/api/base/api_getstableaccesstoken.html
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded, make_client, mask_secret, safe_json

NAME = "微信小程序"
BASE = "https://api.weixin.qq.com"
DISPLAY_FIELD = "appid"

# errcode → (判定, 说明)
_ERRORS = {
    -1: ("error", "微信系统繁忙，请稍后重试"),
    40001: ("invalid", "AppSecret 不正确（或已被重置）"),
    40002: ("error", "grant_type 参数不正确"),
    40013: ("invalid", "AppID 不合法"),
    40125: ("invalid", "AppSecret 无效"),
    40164: ("restricted", "调用方 IP 不在该账号的 IP 白名单内"),
    41002: ("invalid", "缺少 AppID"),
    41004: ("invalid", "缺少 AppSecret"),
    43002: ("error", "请求方式错误（该接口要求 POST）"),
    45009: ("restricted", "已超出该接口的当日调用量上限"),
    45011: ("restricted", "触发接口频率限制（单位时间调用量超限）"),
    48001: ("restricted", "该接口未获授权，请确认小程序已开通对应权限"),
    89503: ("restricted", "该 IP 的调用需管理员授权确认"),
    89506: ("restricted", "该 IP 不在白名单，需管理员确认后方可调用"),
    89507: ("restricted", "管理员已拒绝该 IP 的调用授权"),
}

# 基础信息里这些字段不展示（错误码字段只用于判定；其余是内部标识，对用户无意义）
_BASICINFO_SKIP = {"errcode", "errmsg", "accountid"}


# ── 通用小工具 ───────────────────────────────────────────

def _scalar(value) -> str:
    if value is None or value == [] or value == {}:
        return "—"
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(parts) if parts else "—"
    if isinstance(value, dict):
        parts = [f"{key}={_scalar(val)}" for key, val in value.items()]
        return "、".join(parts) if parts else "—"
    text = str(value).strip()
    return text or "—"


def _scrub(text: str, secrets=()) -> str:
    """把文本里出现的机密值替换成掩码形式（防御性：微信理论上不回显入参）。"""
    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, mask_secret(value))
    return text


def _err(payload: dict, secrets=()) -> tuple:
    """把微信的错误响应转成 ``(中文说明, 判定)``。"""
    try:
        code = int(payload.get("errcode"))
    except (TypeError, ValueError):
        return "微信返回了无法识别的响应", "error"
    verdict, label = _ERRORS.get(code, ("error", ""))
    if label:
        return f"{label}（errcode {code}）", verdict
    message = _scrub(str(payload.get("errmsg") or "").strip(), secrets)
    return f"微信返回错误 {code}：{message or '未知'}", "error"


def _flatten(payload: dict) -> list:
    """把响应体摊成「项目 / 值」两列，容忍字段改名：未知字段按原键名展示。

    标量字段直接成行；嵌套字典展开一层（键名形如 ``父.子``）。这样即便微信调整了字段名，
    用户也能看到原始信息，而不是一片「—」。
    """
    rows: list[dict] = []
    seen: set = set()
    for key, value in payload.items():
        if key in _BASICINFO_SKIP or key in seen:
            continue
        seen.add(key)
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_key in _BASICINFO_SKIP or isinstance(sub_value, (dict, list)):
                    continue
                rows.append({"item": f"{key}.{sub_key}", "value": _scalar(sub_value)})
        elif isinstance(value, list):
            continue
        else:
            rows.append({"item": key, "value": _scalar(value)})
    return rows


def _rows(rows: list, columns, empty_text: str) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows),
        "has_more": False,
        "empty_text": empty_text,
    }


def _fetch_token(client, appid: str, secret: str):
    """取 access_token，返回 ``(token, expires_in, error, verdict)``。

    仅在账号当前无可用 token 时才新签发；已有可用 token 时微信原样返回，不作废任何东西。
    """
    resp = client.post(
        f"{BASE}/cgi-bin/stable_token",
        json={
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
            "force_refresh": False,
        },
    )
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return "", "", f"响应无法解析（HTTP {resp.status_code}）", "error"
    token = str(payload.get("access_token") or "").strip()
    if token:
        try:
            expires = int(payload.get("expires_in"))
        except (TypeError, ValueError):
            expires = 0
        return token, expires, "", ""
    message, verdict = _err(payload, (secret, appid))
    return "", "", message, verdict


# ── 列定义 ───────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]


# ── 操作实现 ─────────────────────────────────────────────

def _h_token(client, appid, secret, params) -> dict:
    token, expires, error, _ = _fetch_token(client, appid, secret)
    if error:
        return {"ok": False, "error": error}
    rows = [
        {"item": "AppID", "value": _scalar(appid)},
        {"item": "调用凭据", "value": mask_secret(token)},
        {"item": "剩余有效期(秒)", "value": str(expires)},
        {"item": "剩余有效期(约)", "value": f"{expires // 3600} 小时 {expires % 3600 // 60} 分"},
        {"item": "说明", "value": "凭据已掩码展示；本次未主动刷新，若账号原有凭据仍有效则原样返回"},
    ]
    return _rows(rows, _KV_COLUMNS, "未能取得调用凭据")


def _h_basicinfo(client, appid, secret, params) -> dict:
    token, _, error, _ = _fetch_token(client, appid, secret)
    if error:
        return {"ok": False, "error": error}
    resp = client.get(
        f"{BASE}/cgi-bin/account/getaccountbasicinfo",
        params={"access_token": token},
    )
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"响应无法解析（HTTP {resp.status_code}）"}
    if int(payload.get("errcode") or 0) != 0:
        message, _ = _err(payload, (secret, token))
        return {"ok": False, "error": message}

    rows = _flatten(payload)
    if not rows:
        return {"ok": False, "error": "微信未返回可用的小程序基础信息"}
    return _rows(rows, _KV_COLUMNS, "微信未返回可用的小程序基础信息")


def _h_quota(client, appid, secret, params) -> dict:
    cgi_path = str(params.get("cgi_path") or "").strip()
    if not cgi_path:
        return {"ok": False, "error": "请填写要查询的接口路径"}
    token, _, error, _ = _fetch_token(client, appid, secret)
    if error:
        return {"ok": False, "error": error}
    resp = client.post(
        f"{BASE}/cgi-bin/openapi/quota/get",
        params={"access_token": token},
        json={"cgi_path": cgi_path},
    )
    payload = safe_json(resp)
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"响应无法解析（HTTP {resp.status_code}）"}
    if int(payload.get("errcode") or 0) != 0:
        message, _ = _err(payload, (secret, token))
        return {"ok": False, "error": message}

    quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
    rows = [
        {"item": "接口路径", "value": cgi_path},
        {"item": "日调用上限", "value": _scalar(quota.get("daily_limit"))},
        {"item": "今日已用", "value": _scalar(quota.get("used"))},
        {"item": "今日剩余", "value": _scalar(quota.get("remain"))},
    ]
    return _rows(rows, _KV_COLUMNS, "未取得配额信息")


_HANDLERS = {
    "token": _h_token,
    "basicinfo": _h_basicinfo,
    "quota": _h_quota,
}


# ── 操作声明 ─────────────────────────────────────────────

def actions(data) -> list:
    """声明可用操作（凭据完整时提供只读查询）。"""
    data = data or {}
    if not (str(data.get("appid") or "").strip() and str(data.get("secret") or "").strip()):
        return []
    return [
        {
            "id": "token",
            "name": "获取调用凭据",
            "kind": "list",
            "group": "凭据",
            "order": 10,
            "description": "确认 AppSecret 可用并查看 access_token 剩余有效期（不主动刷新）",
            "columns": _KV_COLUMNS,
            "fields": [],
            "submit": "获取",
            "empty_text": "未能取得调用凭据",
        },
        {
            "id": "basicinfo",
            "name": "小程序基础信息",
            "kind": "list",
            "group": "账号",
            "order": 20,
            "description": "名称 / 头像 / 主体信息 / 认证状态（字段按微信返回原样列出）",
            "columns": _KV_COLUMNS,
            "fields": [],
            "submit": "查询",
            "empty_text": "微信未返回可用的小程序基础信息",
        },
        {
            "id": "quota",
            "name": "接口调用配额",
            "kind": "list",
            "group": "配额",
            "order": 30,
            "description": "查询指定接口的日调用上限 / 已用量 / 剩余量",
            "columns": _KV_COLUMNS,
            "fields": [
                {"key": "cgi_path", "label": "接口路径", "type": "text", "required": True,
                 "placeholder": "如 /cgi-bin/message/subscribe/send"},
            ],
            "submit": "查询",
            "empty_text": "未取得配额信息",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    异常消息**只带异常类型名**，不带 ``str(exc)``——httpx 的异常文本里含完整请求 URL，
    而 access_token 就在 URL 的 query 里。
    """
    params = params or {}
    data = data or {}
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return {"ok": False, "error": f"未知操作：{action_id}"}
    appid = str(data.get("appid") or "").strip()
    secret = str(data.get("secret") or "").strip()
    if not appid or not secret:
        return {"ok": False, "error": "缺少 AppID 或 AppSecret"}
    try:
        with make_client() as client:
            return handler(client, appid, secret, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 验证 ─────────────────────────────────────────────────

def validate(data) -> list:
    if not data:
        return ["配置数据为空"]
    errors = []
    if not str(data.get("appid") or "").strip():
        errors.append("AppID 不能为空")
    if not str(data.get("secret") or "").strip():
        errors.append("AppSecret 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """验证 AppID / AppSecret：取一次 stable_token，force_refresh=false。"""
    data = data or {}
    appid = str(data.get("appid") or "").strip()
    secret = str(data.get("secret") or "").strip()
    if not appid or not secret:
        return VerifyResult(status="invalid", message="AppID 或 AppSecret 为空")

    with make_client() as client:
        token, expires, error, verdict = _fetch_token(client, appid, secret)

    if token:
        account = {"AppID": appid}
        if expires:
            account["凭据剩余有效期"] = f"{expires // 3600} 小时 {expires % 3600 // 60} 分"
        return VerifyResult(status="valid", message="凭证有效", account_info=account)

    return VerifyResult(status=verdict or "error", message=error)


def get_display_summary(data) -> str:
    data = data or {}
    appid = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {appid}" if appid else NAME
