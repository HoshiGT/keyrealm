"""火山方舟（Volcengine Ark）API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://docs.volcengine.com/docs/ark （火山方舟大模型 API）

**方舟是平台，不是单个模型**：它是字节跳动（火山引擎）的模型托管网关，
`ark.cn-beijing.volces.com` 上同时挂着 **豆包（doubao-*）**、**DeepSeek**、**智谱 GLM**、
**阿里 Qwen**、**Mistral** 等多家模型（2026-10-09 实测：某账号 `/models` 的 135 条里能看到
`glm-4-5-air-*` / `qwen3-8b-*` / `mistral-7b-instruct-*`）。所以本插件**不叫「豆包」**——
凡是在方舟上开的 key（不论用它调哪家模型）都用这一条。

两个必须知道的方舟特性（都是实测，不是文档抄的）：
1. **`/models` 列的是平台基础模型，不含你自己的「推理接入点」**（`ep-xxxxxxxx`）。
   接入点才是很多人实际调用的对象 → 它**不会出现在模型下拉里，必须手输**
   （对话弹窗与「探测使用的模型」两处的模型框都支持直接输入）。
2. **`/models` 里混着大量已关停的历史模型**（实测 135 条里 **83 条 `status == "Shutdown"`**），
   且列表按创建时间排、**首项就是关停款**；同时还混着**嵌入 / 视频生成 / 图像生成**等
   不能对话的模型。因为「默认模型」取的是清单首项，本插件在 `list_models` 里
   **剔除 `Shutdown`、并把非对话类名字排到最后**，免得默认选到一个用不了的模型。

鉴权是标准 Bearer API Key（`Authorization: Bearer <api_key>`）：`/models` 校验该 key
（无鉴权 → 401、伪造 key → 401），所以验证直接用 `/models` 探针即可，无需对话探测。
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
    """方舟可用模型列表（**剔除已关停的**，非对话类排到最后；见文件头）。

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

    **刻意不用通用实现 `make_quota_probe`**：它会「按名字像 flash/lite 的挑一个」再去试，
    而方舟 `/models` 是**过期超集**（实测 135 条里 83 条已关停，另有一堆未开通/找不到的）——
    实测它挑出的全是 `doubao-lite-*` 这些关停款，十个候选全 404、白等 25 秒仍无结论。

    这里改成**与 `list_models` 同源的候选**（已剔除 Shutdown、非对话类沉底）：
    **下拉里能选到的，就是探测会去试的**；凭据里指定的 `probe_model` 优先。逐个试、命中即止。
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
    for model in candidates:
        verdict = quota_chat_probe(base_url, api_key, "openai", preferred_model=model)
        if verdict:
            return verdict
    # 试完仍无结论：写清原因（界面上按 quota_unknown 显示「实测 异常」，不静默）
    log(f"{NAME}：欠费探测试了 {len(candidates)} 个候选都没结论"
        f"——方舟 /models 是过期超集，可在凭据里直接指定「探测使用的模型」")
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
