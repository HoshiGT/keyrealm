"""Dify 应用 API Key 验证插件（plugins/api/ 类别下的独立子插件）。

Dify 服务 API 的密钥是**应用级**的（``Authorization: Bearer app-…``）：一把 key 只对应
Dify 里的**某一个应用**，既列不出工作区也列不出应用清单。所以本插件的能力边界就是
「该应用范围内的读操作 + 该应用支持的对话/工作流执行」。

验证（``verify``）：只读调 ``GET {base_url}/parameters``（「获取应用参数」）。
选它的理由：这是 Dify 服务 API 里少数**既校验 Bearer 令牌、又不产生任何会话或额度消耗**
的读接口——回 200 就等价于「这个 app- 令牌当前可用」，回包还自带该应用的编排形态
（开场白、建议问题、输入表单项、文件上传开关），足以证明它确实挂在某个真实应用上。
验证成功后再 best-effort 打一次 ``GET /info``，把「应用名 + 应用类型」放进 ``account_info``，
用于在同地址的多把 key 之间区分是哪把。

操作（``actions`` 声明、**声明阶段不联网**，可用性由 ``capabilities`` 按 ``/info`` 的
``mode`` 过滤，探测拿不准时 fail-open）：

- ``app_info``：``GET /info`` 应用基本信息（名称/类型/作者/说明/标签）；
- ``app_parameters``：``GET /parameters`` 应用参数（复用 ``verify`` 的解析结果）；
- ``chat``：``POST /chat-messages`` 单轮/多轮流式对话（chat 类应用）；
- ``run_workflow``：``POST /workflows/run`` 触发工作流（workflow 类应用，**有副作用**）。

自带完整的校验与展示逻辑，只复用框架层 backend.plugin_sdk 的通用工具。

参考文档：
- https://docs.dify.ai/en/api-reference/applications/get-app-info
- https://docs.dify.ai/en/api-reference/applications/get-app-parameters
- https://docs.dify.ai/en/api-reference/chat-messages/send-chat-message
- https://docs.dify.ai/en/api-reference/workflow-runs/run-workflow
"""
from __future__ import annotations

import json

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, log, make_client, safe_json

NAME = "Dify"
DEFAULT_BASE_URL = "https://api.dify.ai/v1"
DISPLAY_FIELD = "api_key"

# 401/403 之外的「令牌本身不成立」判定：Dify 用 code=unauthorized / access_denied 表达
INVALID_CODES = {"unauthorized", "access_denied", "invalid_token"}

# Dify 用 user 区分「终端用户」——会话、消息、文件都挂在它下面。
# 插件没有终端用户这个概念，给一个固定值即可（换值会另起一批会话，没必要）。
END_USER = "keyrealm"

# 能用 POST /chat-messages 的应用形态。注意 completion（文本生成应用）走的是
# /completion-messages，不在其列；workflow 只能跑工作流，也不能对话。
_CHAT_MODES = {"chat", "agent-chat", "advanced-chat", "agent"}

# 「字段 / 值」两列的行格式（app_info 与 app_parameters 共用）
_PAIR_COLUMNS = [
    {"key": "item", "label": "字段", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

# /parameters 回包里值得摊开展示的键，及其中文标签（顺序即展示顺序）
_PARAM_ROWS = [
    ("user_input_form_count", "输入项数量"),
    ("suggested_questions_count", "建议问题数"),
    ("opening_statement", "开场白"),
    ("file_upload_enabled", "允许文件上传"),
    ("system_parameters", "系统参数"),
]


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _root(base_url: str) -> str:
    return (base_url or DEFAULT_BASE_URL).rstrip("/")


def _summarize(payload: dict) -> tuple[str, dict]:
    """把 /parameters 的回包压成一行说明 + 一份结构化信息。"""
    info: dict = {}
    parts = ["API Key 有效"]

    form = payload.get("user_input_form")
    if isinstance(form, list) and form:
        info["user_input_form_count"] = len(form)
        parts.append(f"{len(form)} 个输入项")

    questions = payload.get("suggested_questions")
    if isinstance(questions, list) and questions:
        info["suggested_questions_count"] = len(questions)
        parts.append(f"{len(questions)} 条建议问题")

    opening = " ".join(str(payload.get("opening_statement") or "").split())
    if opening:
        info["opening_statement"] = opening[:120]

    file_upload = payload.get("file_upload")
    if isinstance(file_upload, dict) and file_upload.get("enabled") is not None:
        info["file_upload_enabled"] = bool(file_upload.get("enabled"))

    system_parameters = payload.get("system_parameters")
    if isinstance(system_parameters, dict):
        features = sorted(system_parameters.keys())
        if features:
            info["system_parameters"] = features

    return "，".join(parts), info


def _get_info(base_url: str, api_key: str) -> tuple[int, dict]:
    """``GET /info``，返回 ``(HTTP 状态码, 回包 dict)``。

    网络异常统一降级为状态码 0 —— 调用方（``verify`` / ``capabilities``）都把它当
    「拿不准」处理，绝不让一次探测抖动把结论带偏。
    """
    url = f"{_root(base_url)}/info"
    try:
        with make_client() as client:
            resp = client.get(url, headers=_auth_headers(api_key))
        payload = safe_json(resp)
    except Exception as exc:  # noqa: BLE001 - 探测失败只影响展示/过滤，不该冒泡
        log(f"Dify /info 探测失败（{type(exc).__name__}）")
        return 0, {}
    return resp.status_code, payload if isinstance(payload, dict) else {}


def _account_info(base_url: str, api_key: str) -> dict | None:
    """best-effort 取「应用名 + 应用类型」；拿不到就返回 None（由调用方回退到原有信息）。

    只留这两项是有意的：``/parameters`` 那堆计数（输入项、建议问题…）在同地址的多把 key
    之间**完全一样**（同一个应用），区分不了凭据，放列表里就是纯噪音。
    """
    status, payload = _get_info(base_url, api_key)
    if status != 200:
        return None

    info: dict = {}
    name = " ".join(str(payload.get("name") or "").split())
    if name:
        info["name"] = name
    mode = str(payload.get("mode") or "").strip()
    if mode:
        info["mode"] = mode
    return info or None


def _verify_dify(base_url: str, api_key: str) -> VerifyResult:
    url = f"{_root(base_url)}/parameters"
    with make_client() as client:
        resp = client.get(url, headers=_auth_headers(api_key))

    status = resp.status_code
    payload = safe_json(resp) or {}

    if status == 200:
        message, info = _summarize(payload if isinstance(payload, dict) else {})
        # 附加信息整块换成 /info 的结论；/info 打不通就保留上面那份旧信息（verify 不因此失败）
        account = _account_info(base_url, api_key)
        if account:
            info = account
        else:
            info["base_url"] = base_url
        return VerifyResult(status="valid", message=message, account_info=info)

    code = str(payload.get("code") or "").strip().lower()
    detail = str(payload.get("message") or "").strip()
    if status in (401, 403) or code in INVALID_CODES:
        return VerifyResult(
            status="invalid",
            message=f"API Key 无效（HTTP {status}{'，' + detail if detail else ''}）",
        )
    if status == 404:
        return VerifyResult(
            status="error",
            message=f"Base URL 下找不到 /parameters（HTTP 404），请确认地址是否含版本前缀：{base_url}",
        )
    if status == 429:
        return VerifyResult(status="valid", message="API Key 有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, detail or body_snippet(resp))


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get(DISPLAY_FIELD) or "").strip():
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        errors.append("Base URL 需以 http:// 或 https:// 开头")
    return errors


@guarded
def verify(data):
    """验证 Dify 应用 API Key 是否有效。"""
    data = data or {}

    api_key = (data.get(DISPLAY_FIELD) or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    log(f"校验 Dify 凭据（{base_url}）")
    return _verify_dify(base_url, api_key)


# ── 简易操作 ────────────────────────────────────────────────
# actions() 只做「声明」，不联网：菜单要秒出，不能因为一次探测卡住。

def actions(data: dict) -> list[dict]:
    """声明可用操作（**不联网**；真正能不能用由 ``capabilities`` 按应用形态过滤）。"""
    data = data or {}
    if not (data.get(DISPLAY_FIELD) or "").strip():
        return []
    return [
        {
            "id": "app_info",
            "name": "应用信息",
            "kind": "list",
            "group": "应用",
            "order": 10,
            "description": "GET /info（应用名、类型、作者、说明、标签）",
            "columns": _PAIR_COLUMNS,
            "empty_text": "未获取到应用信息",
        },
        {
            "id": "app_parameters",
            "name": "应用参数",
            "kind": "list",
            "group": "应用",
            "order": 20,
            "description": "GET /parameters（开场白、建议问题、输入表单项、文件上传开关）",
            "columns": _PAIR_COLUMNS,
            "empty_text": "未获取到应用参数",
        },
        {
            "id": "chat",
            "name": "对话",
            "kind": "chat",
            "group": "对话",
            "order": 30,
            "description": "POST /chat-messages（流式；仅 chat / agent / chatflow 类应用可用）",
        },
        {
            "id": "run_workflow",
            "name": "运行工作流",
            "kind": "form",
            "group": "工作流",
            "order": 40,
            "description": "POST /workflows/run（流式；仅 workflow 应用可用）",
            # **写操作**：会真的执行一次工作流，产生执行记录、消耗额度（前端据此加二次确认）
            "write": True,
            "confirm": "将真实执行该工作流：会产生执行记录并消耗额度。确定继续？",
            "fields": [
                {
                    "key": "inputs",
                    "label": "输入变量 (JSON)",
                    "type": "textarea",
                    "required": False,
                    "placeholder": '{"query": "你好"}',
                },
                {
                    "key": "user",
                    "label": "用户标识",
                    "required": False,
                    "default": END_USER,
                    "placeholder": END_USER,
                },
            ],
            "submit": "运行",
        },
    ]


def capabilities(data: dict) -> dict:
    """报告当前应用**真正能用**的操作 id（会联网探一次 ``/info``，fail-open）。

    过滤依据只有一个：``/info`` 回包里的 ``mode``。``chat`` 只给能对话的形态，
    ``run_workflow`` 只给 workflow 应用——把跑不通的按钮摆出来只会让人白点一次。

    **拿不准一律放行**（``available=None``，框架按「未知 → 全部可用」处理）：
    网络抖动、自建实例版本老、回包里没有能读懂的 ``mode``，都算拿不准。
    只有「基础身份接口 /info 明确拒了这把 key」才真的返回空列表。
    """
    data = data or {}
    api_key = (data.get(DISPLAY_FIELD) or "").strip()
    if not api_key:
        return {"available": [], "note": "缺少 API Key"}

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    status, payload = _get_info(base_url, api_key)

    if status in (401, 403):
        # /info 是最基础的身份探针，它明确拒绝 = 这把 key 已经废了，
        # 此时没有「拿不准」可言：任何操作都会失败，直接全隐藏。
        return {"available": [], "note": "API Key 无效或已失效"}

    mode = str(payload.get("mode") or "").strip().lower() if status == 200 else ""
    if not mode:
        # 探测失败 / 回包看不懂 → fail-open：探测抖一下就把能用的操作藏掉，比不探测更糟
        return {"available": None, "note": ""}

    available = ["app_info", "app_parameters"]
    if mode in _CHAT_MODES:
        available.append("chat")
    if mode == "workflow":
        available.append("run_workflow")

    # 用 actions() 里声明的名字拼提示，免得两处标签各写各的（actions() 不联网，这里放心调）
    names = {item["id"]: item["name"] for item in actions(data)}
    hidden = [names.get(action_id, action_id) for action_id in ("chat", "run_workflow")
              if action_id not in available]
    note = f"当前应用类型「{mode}」，已隐藏：{'、'.join(hidden)}" if hidden else ""
    return {"available": available, "note": note}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作（只读查询 + 工作流触发）。

    ``chat`` 正常情况下走 ``stream_action``；这里保留一条同步实现，避免框架从同步入口
    调进来时变成一个「未知操作」的空洞。
    """
    data = data or {}
    params = params or {}
    api_key = (data.get(DISPLAY_FIELD) or "").strip()
    if not api_key:
        return {"ok": False, "error": "API Key 为空"}
    base_url = _root(data.get("base_url") or DEFAULT_BASE_URL)

    try:
        if action_id == "app_info":
            return _run_app_info(base_url, api_key)
        if action_id == "app_parameters":
            return _run_app_parameters(base_url, api_key)
        if action_id == "chat":
            return _run_chat(base_url, api_key, params, lambda _t: None)
        if action_id == "run_workflow":
            return _run_workflow(base_url, api_key, params)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行：``chat`` 逐段回调 ``on_delta(text)``，其余操作直接回退到 ``run_action``。"""
    if action_id != "chat":
        return run_action(action_id, params, data)

    data = data or {}
    api_key = (data.get(DISPLAY_FIELD) or "").strip()
    if not api_key:
        return {"ok": False, "error": "API Key 为空", "output": ""}
    base_url = _root(data.get("base_url") or DEFAULT_BASE_URL)
    try:
        return _run_chat(base_url, api_key, params or {}, on_delta or (lambda _t: None))
    except Exception as exc:  # noqa: BLE001 - 流式边界兜底
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}", "output": ""}


# ── 操作实现 ────────────────────────────────────────────────

def _format_value(value) -> str:
    """把回包里的值压成单行可读文本。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value) or "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = " ".join(str(value).split())
    return text or "—"


def _http_failure(resp, label: str) -> dict:
    """把非 200 的 HTTP 回包转成统一的 ``{"ok": False, "error": ...}``。"""
    payload = safe_json(resp) or {}
    code = str(payload.get("code") or "").strip()
    detail = str(payload.get("message") or "").strip() or body_snippet(resp)
    if resp.status_code in (401, 403) or code.lower() in INVALID_CODES:
        return {"ok": False, "error": f"API Key 无效或无权访问（HTTP {resp.status_code}）"}
    tail = f"：{detail}" if detail else ""
    return {"ok": False, "error": f"{label}失败（HTTP {resp.status_code}）{tail}"}


def _pair_frame(rows: list[dict], empty_text: str) -> dict:
    """「字段 / 值」两列列表的统一回包。"""
    if not rows:
        return {"ok": True, "rows": [], "columns": _PAIR_COLUMNS, "total": 0, "empty_text": empty_text}
    return {"ok": True, "rows": rows, "columns": _PAIR_COLUMNS, "total": len(rows), "has_more": False}


def _run_app_info(base_url: str, api_key: str) -> dict:
    """``GET /info``：应用基本信息，摊成「字段 / 值」两列。"""
    with make_client() as client:
        resp = client.get(f"{base_url}/info", headers=_auth_headers(api_key))
    if resp.status_code != 200:
        return _http_failure(resp, "获取应用信息")

    payload = safe_json(resp) or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "应用信息回包无法解析"}

    tags = payload.get("tags")
    tag_text = "、".join(str(t) for t in tags if t) if isinstance(tags, list) else ""
    rows = [
        {"item": label, "value": _format_value(payload.get(key))}
        for label, key in (
            ("名称", "name"),
            ("应用类型", "mode"),
            ("作者", "author_name"),
            ("说明", "description"),
        )
    ]
    rows.append({"item": "标签", "value": tag_text or "—"})
    return _pair_frame(rows, "未获取到应用信息")


def _run_app_parameters(base_url: str, api_key: str) -> dict:
    """``GET /parameters``：复用 ``_summarize`` 的产物，摊成「字段 / 值」两列。"""
    with make_client() as client:
        resp = client.get(f"{base_url}/parameters", headers=_auth_headers(api_key))
    if resp.status_code != 200:
        return _http_failure(resp, "获取应用参数")

    payload = safe_json(resp) or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "应用参数回包无法解析"}

    _, info = _summarize(payload)
    rows = [
        {"item": label, "value": _format_value(info.get(key))}
        for key, label in _PARAM_ROWS
        if key in info
    ]
    return _pair_frame(rows, "应用没有可展示的参数")


def _iter_sse(resp):
    """逐行解析 SSE，产出 ``data:`` 后面的 JSON 对象（空行 / 心跳 / 坏 JSON 一律跳过）。

    Dify 的 SSE 每条同时带 ``event:`` 与 ``data:``，而 ``data:`` 里的 JSON 自己也带
    ``event`` 字段 —— 只认 ``data:`` 这一路，避免两处解析结果对不上。
    """
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", "ignore")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _sse_error(obj: dict) -> str:
    """把流内的 ``error`` 事件转成人话。

    **注意**：应用侧报错是以 HTTP 200 + ``event=error`` 的形态在流里下发的，
    光看状态码会把它当成成功，必须在这里显式拦下来。
    """
    code = str(obj.get("code") or "").strip()
    detail = str(obj.get("message") or "").strip()
    status = obj.get("status")
    head = f"应用返回错误（{code or status or 'unknown'}）"
    return f"{head}：{detail}" if detail else head


def _last_user_text(messages) -> str:
    """取最后一条用户消息作为 ``query``（Dify 的 /chat-messages 一次只吃一句）。"""
    if isinstance(messages, str):
        return messages.strip()
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("role") or "") == "user":
            return str(message.get("content") or "").strip()
    # 没标 role 的（或被上游裁过的）就退而取最后一条有内容的消息
    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("content") or "").strip():
            return str(message["content"]).strip()
    return ""


def _merge_answer(parts: list, chunk: str) -> bool:
    """把新片段并入已累计文本，返回是否有变化。

    Agent 模式收尾的那个 ``agent_message`` 会**整段重发完整答案**（不是增量），
    无脑 append 会得到「答案答案」。这里用前缀关系识别：新片段若以已累计文本开头，
    就当作覆盖而不是追加（正常增量永远不满足这个条件，最坏情况也只是多一次字符串比较）。
    """
    if not chunk:
        return False
    full = "".join(parts)
    if chunk == full:
        return False
    if full and chunk.startswith(full):
        parts[:] = [chunk]
        return True
    parts.append(chunk)
    return True


def _run_chat(base_url: str, api_key: str, params: dict, emit) -> dict:
    """``POST /chat-messages`` 流式对话：``message`` / ``agent_message`` 的 answer 逐段回吐。

    **上下文说明**：Dify 靠 ``conversation_id`` 维系多轮上下文，而插件在一次流式调用里
    拿不到上一轮的 id（凭据上没有能持久化它的字段），所以每轮都是一次全新会话；
    ``query`` 只取最后一条用户消息，界面里保留的历史不会喂给应用。
    """
    query = _last_user_text(params.get("messages"))
    if not query:
        return {"ok": False, "error": "请输入要发送的内容", "output": ""}

    body = {
        "inputs": {},
        "query": query,
        "user": str(params.get("user") or "").strip() or END_USER,
        "response_mode": "streaming",
        # 关掉自动起标题：它会另起一次 LLM 调用，message_end 之后还要多吊住连接 5~10 秒
        "auto_generate_name": False,
    }

    parts: list[str] = []
    compose_error = ""
    with make_client() as client:
        with client.stream(
            "POST",
            f"{base_url}/chat-messages",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json=body,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                failure = _http_failure(resp, "对话")
                failure["output"] = ""
                return failure

            for obj in _iter_sse(resp):
                event = str(obj.get("event") or "")
                if event in ("message", "agent_message"):
                    chunk = str(obj.get("answer") or "")
                    if _merge_answer(parts, chunk):
                        emit(chunk)
                elif event == "message_end":
                    # 终态明确到了就主动收工，不用等连接关闭
                    break
                elif event == "error":
                    # HTTP 200 里的流内报错，必须自己判
                    return {"ok": False, "error": _sse_error(obj), "output": "".join(parts)}
                elif event == "workflow_finished":
                    # chatflow 类应用（advanced-chat）还会把**编排层**的成败放在这里，
                    # 且同样藏在 HTTP 200 里：这时流里可能一个 message 事件都没有，
                    # 光看「没收到内容」会把真正的原因（如某个输入变量没填）丢掉。
                    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
                    detail = str(data.get("error") or "").strip()
                    state = str(data.get("status") or "").strip()
                    if detail or state in ("failed", "stopped"):
                        compose_error = (
                            f"应用编排执行失败（{state or 'failed'}）：{detail or '上游未给出原因'}"
                        )

    output = "".join(parts)
    if not output.strip():
        if compose_error:
            return {"ok": False, "error": compose_error, "output": ""}
        return {"ok": False, "error": "应用没有返回任何内容", "output": ""}
    return {"ok": True, "output": output.strip()}


def _parse_inputs(raw) -> tuple[dict, str]:
    """把用户填的 JSON 文本解析成 ``inputs`` 对象，返回 ``(对象, 错误信息)``。"""
    if isinstance(raw, dict):  # 上层直接给了对象
        return raw, ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return {}, f"输入变量不是合法 JSON：{exc}"
    if not isinstance(parsed, dict):
        return {}, "输入变量需是一个 JSON 对象（{...}）"
    return parsed, ""


def _run_workflow(base_url: str, api_key: str, params: dict) -> dict:
    """``POST /workflows/run``（流式）：等到 ``workflow_finished``，把 outputs 打出来。

    **有副作用**：每执行一次都会在工作流日志里留下一条执行记录并消耗额度，
    所以 ``actions()`` 里这条声明了 ``write: true``，前端会先做二次确认。
    """
    inputs, error = _parse_inputs(params.get("inputs"))
    if error:
        return {"ok": False, "error": error}

    body = {
        "inputs": inputs,
        "response_mode": "streaming",
        "user": str(params.get("user") or "").strip() or END_USER,
    }

    finished: dict | None = None
    node_count = 0
    with make_client() as client:
        with client.stream(
            "POST",
            f"{base_url}/workflows/run",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json=body,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                return _http_failure(resp, "运行工作流")

            for obj in _iter_sse(resp):
                event = str(obj.get("event") or "")
                if event == "workflow_finished":
                    data = obj.get("data")
                    finished = data if isinstance(data, dict) else obj
                    break
                if event == "node_finished":
                    node_count += 1
                elif event == "error":
                    return {"ok": False, "error": _sse_error(obj)}

    if not finished:
        return {"ok": False, "error": "工作流没有返回结束事件（可能中途断开）"}

    status = str(finished.get("status") or "").strip()
    failure = str(finished.get("error") or "").strip()
    if failure or status in ("failed", "stopped"):
        return {"ok": False, "error": f"工作流执行失败（{status or 'failed'}）：{failure or '上游未给出原因'}"}

    outputs = finished.get("outputs")
    lines = [f"状态：{status or 'succeeded'}"]
    if node_count:
        lines.append(f"执行节点：{node_count} 个")
    elapsed = finished.get("elapsed_time")
    if isinstance(elapsed, (int, float)):
        lines.append(f"耗时：{elapsed:.2f} 秒")
    if isinstance(outputs, dict) and outputs:
        lines.append("输出：\n" + json.dumps(outputs, ensure_ascii=False, indent=2))
    else:
        lines.append("输出：（工作流没有返回输出）")
    return {"ok": True, "output": "\n".join(lines)}


def get_display_summary(data):
    """凭据列表中的简短展示文本。"""
    data = data or {}
    return f"{NAME} | {(data.get('base_url') or '').strip() or DEFAULT_BASE_URL}"
