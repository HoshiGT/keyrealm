"""火山方舟（Volcengine Ark）API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://docs.volcengine.com/docs/ark （火山方舟大模型 API）

**方舟是平台，不是单个模型**：它是字节跳动（火山引擎）的模型托管网关，
`ark.cn-beijing.volces.com` 上同时挂着 **豆包（doubao-*）**、**DeepSeek**、**智谱 GLM**、
**阿里 Qwen**、**Mistral** 等多家模型。因此同一把 key（不论用它调哪家模型）都归本插件。

两个必须知道的方舟特性：
1. **`/models` 列的是平台基础模型，不含「推理接入点」**（`ep-xxxxxxxx`）。接入点往往是实际
   调用的对象，**不会出现在模型下拉里，必须手输**（对话弹窗与「探测使用的模型」都支持直接输入）。
2. **`/models` 会随时间变，且不全是「当前可调的」**：已关停条目占比很高（例如某次拉取 135 条中
   83 条为 `Shutdown`，且按创建时间排序、**首项通常是已关停款**），另有 `ModelNotOpen`
   （存在但未开通）等条目。因此本插件**剔除 `Shutdown`、把非对话类排到最后**，并以**刷新到的
   这份清单**作为探测候选；已知确切可用的模型（含接入点 `ep-xxx`）时，在凭据里指定
   「探测使用的模型」即可只试它一个。
3. **欠费探测的候选 = 刷新模型列表看到的那份**（已滤 `Shutdown`）；逐个试、命中即止，
   全试完如实报「异常」并把试过的模型写进日志。

鉴权是标准 Bearer API Key（`Authorization: Bearer <api_key>`）：`/models` 校验该 key
（无鉴权 → 401、伪造 key → 401），因此验证直接用 `/models` 探针，无需对话探测。
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded, log
from llm_common import probe_requested, quota_unknown
from llm_protocols import (probe_openai_models, chat_completion, model_items,
                           chat_completion_stream, resolve_chat_model, quota_chat_probe)

NAME = "火山方舟"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
PROTOCOL = "openai_models"

# 欠费探测见文件下方的 `quota_probe()` —— 不走 `make_quota_probe`，理由写在那里

# 方舟清单里**不能对话**的模型家族（嵌入 / 视频生成 / 图像生成 / 语音 / 翻译）。
# 它们只是**排到最后**、并不从下拉里删掉——用户仍可能想选（也仍可手输接入点）。
# 注意别把 `vision` 放进来：方舟的 `doubao-*-vision-*` 是**多模态对话模型**，能聊天。
_NON_CHAT_HINTS = ("embedding", "embed", "seedance", "seedream", "tts", "asr",
                   "rerank", "moderation", "translation")


def _base_url(data: dict) -> str:
    """实际使用的 Base URL：凭据填写优先，留空取默认地址。"""
    return ((data or {}).get("base_url") or "").strip() or DEFAULT_BASE_URL


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写 Base URL")
    return errors


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = _base_url(data)
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    return probe_openai_models(base_url, api_key)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """方舟 `/models` 的模型清单（**已剔除 `Shutdown`、非对话类沉底**）。

    ⚠️ 这份清单**会随时间变**，且**不全是「当前就能调的」**：已关停条目占比很高（已滤掉），
    另有 `ModelNotOpen`（存在但未开通）等条目 —— 因此对话/探测里**个别条目仍可能失败，属正常**，
    「逐个试、命中即止」正是为它设计的。
    另外：**推理接入点 `ep-xxxxxxxx` 永远不会出现在这里**，需要手输。

    ``force=True``：用户点了「刷新模型列表」，穿透 24h 模型清单缓存。
    """
    data = data or {}
    names: list[str] = []
    for item in model_items("openai", _base_url(data), (data.get("api_key") or "").strip(), force):
        # 已关停的列出来只会让人选到死模型
        if str(item.get("status") or "").strip().lower() == "shutdown":
            continue
        name = str(item.get("id") or "").strip()
        if name and name not in names:
            names.append(name)
    # 默认模型取的是本清单首项 → 让「能对话的」排在前面（非对话类沉底）
    names.sort(key=lambda n: (any(h in n.lower() for h in _NON_CHAT_HINTS), n))
    return names


# 欠费探测最多试几个候选（每个都是一发 max_tokens=1 的请求）
_PROBE_MAX_CANDIDATES = 6


def quota_probe(data: dict) -> Optional[dict]:
    """欠费探测（凭据开关 ``probe_chat``，默认关）。

    **候选＝本插件 `list_models`**，也就是用户点「刷新模型列表」时看到的那一份：
    已**剔除 `Shutdown`**（该清单里已关停条目占比很高，且按创建时间排序、首项通常是关停款）、
    非对话类沉底。这样「**探测会试的 ＝ 刷新能看到的**」，不会对着早已作废的款发请求；
    凭据里指定了 `probe_model` 就只试它一个。

    ⚠️ 方舟的清单**会随时间变、也不全是「现在就能调的」**（仍有 `ModelNotOpen` 等条目），
    所以是「**逐个试、命中即止**」；一次最多 `_PROBE_MAX_CANDIDATES` 个，全试完就如实报
    「异常」并把试过的模型写进日志。
    """
    data = data or {}
    if not probe_requested(data):
        return None
    base_url = _base_url(data)
    api_key = (data.get("api_key") or "").strip()
    if not base_url or not api_key:
        return None
    preferred = str(data.get("probe_model") or "").strip()
    candidates = ([preferred] if preferred else []) + [
        m for m in list_models(data) if m != preferred
    ]
    candidates = candidates[:_PROBE_MAX_CANDIDATES]
    if not candidates:
        return quota_unknown()
    for model in candidates:
        verdict = quota_chat_probe(base_url, api_key, "openai", preferred_model=model)
        if verdict:
            return verdict
    log(f"{NAME}：欠费探测试了 {len(candidates)} 个候选都没结论（{'、'.join(candidates)}）"
        f"——方舟的清单会变、也不全是当前可调的，可在凭据里指定「探测使用的模型」")
    return quota_unknown()


# ── 可选操作：简单对话 ──────────────────────────────────


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 API Key 时才提供简单对话）。"""
    if not (data or {}).get("api_key"):
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": ""}]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    base_url = _base_url(data)
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    params = params or {}
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
    )


def stream_action(action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行操作：简单对话（每段文本调用 on_delta(text)）。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    params = params or {}
    base_url = _base_url(data)
    if not base_url:
        return {"ok": False, "error": "需要填写 Base URL"}
    model = resolve_chat_model(params.get("model"), lambda: list_models(data))
    if not model:
        return {"ok": False, "error": "无法确定模型：请先选择模型（自动获取模型列表失败）"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        model,
        params.get("messages") or [],
        protocol="openai",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    return f"{NAME} | {_base_url(data or {})}"
