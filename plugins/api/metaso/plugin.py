"""秘塔 AI 搜索（Metaso）API Key 验证插件（plugins/api/ 类别下的独立子插件）。

参考文档：https://metaso.cn/search-api/playground

自带完整的校验与展示逻辑，只复用 api_common 的通用工具；
复制本目录即可派生出新的服务插件。

本插件提供：

- **verify**：对 ``POST {base}/search`` 发一次最小搜索（``q`` + ``size=1``）判断 Key 有效性。
  ⚠️ 秘塔**成功与失败都是 HTTP 200**：鉴权失败是 ``{"errCode":2005,"errMsg":"API密钥无效"}``、
  余额不足是 ``{"errCode":3000,"errMsg":"余额不足"}``。所以判定必须看**响应体的 errCode**，
  只看状态码会把欠费当成功。``errCode 3000`` 属「密钥有效但账户欠费」→ 仍判 ``valid``。
- **account_info**：**不提供**。秘塔没有已证实的余额/用量接口（`/usage` 即使存在，响应结构也
  始终没能确认），按 §2.2「宁可不显示也不编」的原则不做探测；欠费信息由 `verify` 的 message 承载。
- **actions**：
  - ``search``（``list``）：多维搜索，表格列 标题/链接/日期/相关度，行内「阅读」下钻到 ``read``；
  - ``read``（``form``）：网页解析（``POST /reader``），返回 markdown 正文。
- **capabilities**：先探搜索；搜索明确不可用时再单独探一次阅读（**两者的额度可能是分开的**，
  实测存在「搜索欠费、阅读仍可用」的 Key），按结果收敛 ``available``；原因不明一律 fail-open。

端点、参数名与错误码均来自**真机实测**（``q`` 是参数名，`query` 会返回「系统内部错误」；
``GET /search`` 返回 405，只支持 POST）。
"""
from api_common import VerifyResult, body_snippet, guarded, make_client, safe_json

NAME = "秘塔 AI 搜索"
DEFAULT_BASE_URL = "https://metaso.cn/api/v1"

# 搜索范围：webpage 已实测可用；其余取自秘塔开放平台 curl 示例/社区实现的 scope 词表。
SCOPES = ["webpage", "document", "scholar", "image", "video", "podcast"]

# scope → 结果数组字段名（各来源命名不一致，取不到时再退化为「扫第一个对象列表」）
_SCOPE_RESULT_KEYS = {
    "webpage": "webpages",
    "document": "documents",
    "scholar": "scholars",
    "image": "images",
    "video": "videos",
    "podcast": "podcasts",
}

# 实测确认的业务错误码。errCode 一律 != 0 才算失败，见 _body_error。
ERR_INVALID_KEY = 2005     # API密钥无效
ERR_INSUFFICIENT = 3000    # 余额不足

# 错误文案关键词：错误码表不全，按文案兜底归类。
_INVALID_HINTS = ("密钥", "api key", "认证", "鉴权", "授权", "unauthorized", "invalid", "token")
_INSUFFICIENT_HINTS = ("余额", "额度", "欠费", "点数", "积分", "credit", "balance",
                       "insufficient", "exhaust", "欠款")
_RATE_HINTS = ("频繁", "限流", "rate", "too many", "throttl")

INSUFFICIENT_MESSAGE = "凭证有效，但账户余额不足"


# ── 通用小工具 ──────────────────────────────────────────

def _headers(api_key: str, accept: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": accept,
    }


def _base_url(data: dict) -> str:
    return ((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL


def _post(client, base_url: str, path: str, api_key: str, body: dict, accept: str = "application/json"):
    return client.post(base_url.rstrip("/") + path, headers=_headers(api_key, accept), json=body)


def _search_body(query: str, size, scope: str = "webpage", include_summary: bool = False) -> dict:
    """搜索请求体。参数名是 ``q``（实测 ``query`` 会返回「系统内部错误」）。"""
    return {"q": query, "scope": scope, "size": size,
            "includeSummary": include_summary, "includeRawContent": False}


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _body_error(payload) -> tuple:
    """从响应体里取 (errCode, errMsg)；无错误码则返回 ``(None, "")``。

    秘塔把业务错误塞在 HTTP 200 的 body 里，两种信封都见过：
    ``{"errCode":2005,"errMsg":"API密钥无效"}`` 与 ``{"code":5000,"message":"系统内部错误"}``。
    **只有 0 / 200 / 缺省算成功**——其余一律当失败。
    """
    if not isinstance(payload, dict):
        return None, ""
    for code_key, msg_key in (("errCode", "errMsg"), ("code", "message")):
        if code_key not in payload:
            continue
        code = payload.get(code_key)
        if code is None or str(code).strip() in ("", "0", "200"):
            continue
        return code, str(payload.get(msg_key) or "").strip()
    return None, ""


def _matches(code, message: str, hints) -> bool:
    return any(hint in (message or "").lower() for hint in hints)


def _is_invalid(code, message: str) -> bool:
    return str(code) == str(ERR_INVALID_KEY) or _matches(code, message, _INVALID_HINTS)


def _is_insufficient(code, message: str) -> bool:
    return str(code) == str(ERR_INSUFFICIENT) or _matches(code, message, _INSUFFICIENT_HINTS)


def _classify_error(code, message: str) -> VerifyResult:
    """把业务错误归类成 valid / invalid / error。

    欠费**不算失效**（见 PLUGIN_GUIDE §3）：仍判 valid，把原因写进 message。
    """
    if _is_invalid(code, message):
        return VerifyResult(status="invalid", message=f"凭证无效：{message or '鉴权失败'}")
    if _is_insufficient(code, message):
        return VerifyResult(status="valid", message=INSUFFICIENT_MESSAGE)
    if _matches(code, message, _RATE_HINTS):
        return VerifyResult(status="valid", message=f"凭证有效，但触发限流：{message}")
    return VerifyResult(
        status="error",
        message=f"无法确认凭证状态 (errCode {code})" + (f"：{message}" if message else ""),
    )


def _as_size(value, default: int = 10, low: int = 1, high: int = 100) -> int:
    """条数兼容 int / 数字字符串；非法或越界时回落默认值。"""
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(count, high))


# ── 校验 ────────────────────────────────────────────────

def _looks_like_search_response(payload) -> bool:
    """成功响应长这样：结果数组平铺在顶层 + ``total`` / ``credits`` / ``searchParameters``。

    判据不能用「结果列表非空」——查一个冷门词返回 0 条也是成功。
    """
    if not isinstance(payload, dict):
        return False
    markers = ("webpages", "documents", "scholars", "images", "videos", "podcasts",
               "total", "credits", "searchParameters", "summary")
    if any(key in payload for key in markers):
        return True
    nested = payload.get("data")
    return isinstance(nested, dict) and any(key in nested for key in markers)


def _verify_metaso(base_url: str, api_key: str) -> VerifyResult:
    """打一次最小搜索判断 Key 有效性（只读，会消耗少量搜索点数）。"""
    with make_client() as client:
        resp = _post(client, base_url, "/search", api_key, _search_body("hello", 1))
        status = resp.status_code
        snippet = body_snippet(resp)
        payload = safe_json(resp)

    # 秘塔的业务错误都裹在 HTTP 200 里，所以非 200 反而是「非预期」——只认 401/403，
    # 其余一律 _unconfirmed，不硬猜（实测未见过 402/429 这类状态码）。
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status != 200:
        return _unconfirmed(status, snippet)

    code, message = _body_error(payload)
    if code is not None:
        return _classify_error(code, message)

    if _looks_like_search_response(payload):
        return VerifyResult(status="valid", message="凭证有效")
    # HTTP 200、既没有业务错误码、也没捞到结果列表 —— 不轻易下结论
    return VerifyResult(status="error", message=f"响应异常：{snippet}")


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    return errors


@guarded
def verify(data):
    """验证秘塔 AI 搜索 API Key 是否有效。"""
    data = data or {}

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    return _verify_metaso(_base_url(data), api_key)


# ── 搜索结果解析 ────────────────────────────────────────

def _pick_results(payload, scope: str) -> list:
    """按 scope 取结果数组；字段名对不上时退化为「扫第一个对象列表」。"""
    if not isinstance(payload, dict):
        return []
    key = _SCOPE_RESULT_KEYS.get(scope or "webpage", "webpages")
    for name in (key, "webpages", "documents", "scholars", "images", "videos", "podcasts"):
        value = payload.get(name)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return [item for item in value if isinstance(item, dict)]
    return []


def _norm(item: dict) -> dict:
    """把各 scope 的字段差异抹平成统一的表格行。"""
    title = " ".join(str(item.get("title") or "").split()) or "(无标题)"
    link = str(item.get("link") or item.get("url") or "").strip()
    date = " ".join(str(item.get("date") or item.get("displayDate") or "").split())
    score = " ".join(str(item.get("score") or "").split())
    snippet = " ".join(str(item.get("snippet") or item.get("abstract") or "").split())
    # snippet 暂不作为列展示，但留在行数据里——想加一列只需补 columns
    return {"title": title, "link": link, "date": date, "score": score, "snippet": snippet}


# ── 可选操作 ────────────────────────────────────────────

def actions(data):
    """声明可用操作（凭据含 api_key 时提供搜索与网页解析）。"""
    data = data or {}
    if not (data.get("api_key") or "").strip():
        return []
    return [
        {
            "id": "search",
            "name": "搜索",
            "kind": "list",
            "group": "查询",
            "order": 10,
            "submit": "搜索",
            "description": "秘塔多维搜索：网页 / 文库 / 学术 / 图片 / 视频 / 播客",
            "columns": [
                {"key": "title", "label": "标题"},
                {"key": "link", "label": "链接", "mono": True},
                {"key": "date", "label": "日期", "width": 120},
                {"key": "score", "label": "相关度", "width": 80, "type": "tag"},
            ],
            "row_actions": [
                {"id": "read", "label": "阅读", "params": {"url": "$row.link"}},
            ],
            "empty_text": "未找到相关结果",
            "fields": [
                {"key": "query", "label": "搜索词", "type": "text",
                 "placeholder": "例如：大模型推理优化", "required": True},
                {"key": "scope", "label": "范围", "type": "select",
                 "options": SCOPES, "default": "webpage"},
                {"key": "size", "label": "条数", "type": "text", "default": "10"},
                {"key": "include_summary", "label": "综合摘要", "type": "select",
                 "options": ["否", "是"], "default": "否"},
            ],
        },
        {
            "id": "read",
            "name": "网页解析",
            "kind": "form",
            "group": "查询",
            "order": 20,
            "submit": "解析",
            "description": "读取指定 URL 的正文（markdown）",
            "fields": [
                {"key": "url", "label": "网页 URL", "type": "text",
                 "placeholder": "https://example.com/article", "required": True},
            ],
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：搜索 / 网页解析（只读，不回显 API Key）。"""
    data = data or {}
    params = params or {}
    try:
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "缺少 API Key"}
        base_url = _base_url(data)

        if action_id == "search":
            return _run_search(base_url, api_key, params)
        if action_id == "read":
            return _run_read(base_url, api_key, params)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def _run_search(base_url: str, api_key: str, params: dict) -> dict:
    query = " ".join(str(params.get("query") or "").split())
    if not query:
        return {"ok": False, "error": "请填写搜索词"}
    scope = (str(params.get("scope") or "").strip() or "webpage")
    size = _as_size(params.get("size"), 10, 1, 100)
    want_summary = str(params.get("include_summary") or "").strip() in ("是", "true", "True", "1")

    with make_client() as client:
        resp = _post(client, base_url, "/search", api_key,
                     _search_body(query, size, scope, want_summary))
    if resp.status_code != 200:
        return {"ok": False, "error": f"搜索失败（HTTP {resp.status_code}）：{body_snippet(resp)}"}

    payload = safe_json(resp)
    code, message = _body_error(payload)
    if code is not None:
        reason = INSUFFICIENT_MESSAGE if _is_insufficient(code, message) else (
            f"鉴权失败：{message}" if _is_invalid(code, message) else (message or "未知错误"))
        return {"ok": False, "error": f"搜索失败（errCode {code}）：{reason}"}

    payload = payload if isinstance(payload, dict) else {}
    rows = [_norm(item) for item in _pick_results(payload, scope)]
    total = payload.get("total")

    # list 表格不渲染 output：综合摘要就作为首行呈现（该行无链接，行内「阅读」自动置灰）
    summary = payload.get("summary")
    if want_summary and isinstance(summary, str) and summary.strip():
        rows.insert(0, {"title": "（综合摘要）" + " ".join(summary.split()),
                        "link": "", "date": "", "score": "", "snippet": ""})

    return {
        "ok": True,
        "rows": rows,
        "empty_text": "未找到相关结果",
        "total": total if isinstance(total, int) else len(rows),
    }


def _run_read(base_url: str, api_key: str, params: dict) -> dict:
    target = str(params.get("url") or "").strip()
    if not target:
        return {"ok": False, "error": "请填写网页 URL"}

    with make_client() as client:
        resp = _post(client, base_url, "/reader", api_key, {"url": target})
    if resp.status_code != 200:
        return {"ok": False, "error": f"解析失败（HTTP {resp.status_code}）：{body_snippet(resp)}"}

    payload = safe_json(resp)
    # 先判错误码：秘塔的错误也是 HTTP 200，绝不能被正文盖过去
    code, message = _body_error(payload)
    if code is not None:
        reason = INSUFFICIENT_MESSAGE if _is_insufficient(code, message) else (message or "未知错误")
        return {"ok": False, "error": f"解析失败（errCode {code}）：{reason}"}

    title = ""
    if isinstance(payload, dict):
        title = " ".join(str(payload.get("title") or "").split())
        markdown = str(payload.get("markdown") or payload.get("content") or "").strip()
    else:
        # 上游按 Accept: text/plain 直接吐 markdown 的情况
        try:
            markdown = (resp.text or "").strip()
        except Exception:  # noqa: BLE001
            markdown = ""

    if not markdown:
        return {"ok": False, "error": "解析失败：接口未返回正文"}

    head = f"{target} 解析成功" + (f"（{title}）" if title else "")
    return {"ok": True, "output": f"{head}，正文前 1500 字：\n\n{markdown[:1500]}"}


def capabilities(data: dict) -> dict:
    """先探搜索，搜索明确不可用时再单独探阅读——**搜索与阅读的额度可能是分开的**
    （实测存在「搜索欠费、阅读仍可用」的 Key），所以要分开判断。

    原因不明（非超时/非鉴权/非欠费的异常返回）一律 fail-open，避免一次抖动把功能全藏了。
    """
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return {"available": [], "note": "缺少 API Key"}
    base_url = _base_url(data)

    try:
        with make_client() as client:
            search_ok, code, message = _probe(client, base_url, api_key,
                                              "/search", _search_body("ping", 1))
            if search_ok:
                # 搜索可用即认为阅读同样可用（实测未见过反例），省下一次计费调用
                return {"available": ["search", "read"]}
            if _is_invalid(code, message):
                return {"available": [], "note": f"API Key 无效：{message or '鉴权失败'}"}
            if not _is_insufficient(code, message):
                return {"available": None}

            read_ok, _, read_message = _probe(client, base_url, api_key, "/reader",
                                              {"url": "https://example.com"})
    except Exception:  # noqa: BLE001 - 网络抖动 → 未知 → 全部可用
        return {"available": None}

    if read_ok:
        return {"available": ["read"],
                "note": f"搜索不可用：{message or '额度不足'}（网页解析仍可用）"}
    return {"available": [],
            "note": f"搜索与网页解析均不可用：{message or read_message or '额度不足'}"}


def _probe(client, base_url: str, api_key: str, path: str, body: dict) -> tuple:
    """只读探测：返回 (是否可用, errCode, errMsg)。"""
    resp = _post(client, base_url, path, api_key, body)
    if resp.status_code != 200:
        return False, resp.status_code, f"HTTP {resp.status_code}"
    code, message = _body_error(safe_json(resp))
    if code is not None:
        return False, code, message
    return True, None, ""


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    return f"{NAME} | {_base_url(data)}"
