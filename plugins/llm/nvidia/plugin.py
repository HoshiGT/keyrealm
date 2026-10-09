"""NVIDIA NIM API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

``GET /v1/models`` 不执行鉴权，不能用于确认 API Key 有效性；鉴权探针使用
``POST /v1/chat/completions``。有效凭据通常返回 200，错误凭据返回 401/403，
账户额度不足可能返回 402/429。

NVIDIA 的模型可用性因账号而异，``/v1/models`` 可能包含已下线或未授权模型，
此类模型会返回 404/410。候选模型在运行时从模型列表筛选，按 24 小时缓存；
探针逐个尝试候选并设置短超时，避免单个不可用模型阻塞验证。

``reasoning_content`` 不在部分模型的 OpenAPI ``Message`` schema 中，响应处理应将其视为可选字段。

参考文档：https://docs.api.nvidia.com/nim/reference/
参考文档：https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-inkling.html
"""
from __future__ import annotations

import httpx

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import probe_requested, quota_unknown
from llm_protocols import (
    probe_openai_chat,
    chat_completion,
    fetch_models,
    chat_completion_stream,
    quota_chat_probe,
    probe_model_candidates,
    resolve_chat_model,
)

NAME = "NVIDIA NIM"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
PROTOCOL = "openai_chat"

# 单个候选超时设为 10 秒，避免不可用模型延长整体探测。
PROBE_TIMEOUT = 10.0


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


def _candidates(data: dict) -> list[str]:
    """构造探针候选，将凭据指定模型置于首位，其后接运行时模型列表候选。

    模型列表按 24 小时缓存，用户刷新时穿透缓存；多候选与短超时用于应对账号不可用模型。
    """
    data = data or {}
    preferred = str(data.get("probe_model") or "").strip()
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    api_key = (data.get("api_key") or "").strip()
    dynamic = probe_model_candidates(base_url, api_key, "openai")
    return ([preferred] if preferred else []) + [m for m in dynamic if m != preferred]


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    tried: list[str] = []
    reason = ""
    candidates = _candidates(data)
    if not candidates:
        return VerifyResult(
            status="error",
            message=f"拿不到候选模型（{base_url} 的模型列表为空），无法验证",
        )
    for model in candidates:
        tried.append(model)
        try:
            result = probe_openai_chat(base_url, api_key, model, timeout=PROBE_TIMEOUT)
        except httpx.TimeoutException:
            reason = f"候选无响应（超时 {int(PROBE_TIMEOUT)} 秒）"
            continue
        except httpx.HTTPError as exc:
            reason = f"网络请求失败（{type(exc).__name__}）"
            continue
        # 200 表示有效；401/403 表示密钥无效，无需继续尝试其他模型
        if result.status in ("valid", "invalid"):
            return result
        # 404 / 410 / 5xx 等：这个模型不可用，换下一个候选
        reason = result.message
    # 压掉末次响应里与固定开头重复的措辞，只留差异部分（如 HTTP 410）
    detail = reason.replace("无法确认凭证状态", "").strip(" ()")
    message = f"候选模型均不可用，未能确认凭证状态（已试 {'、'.join(tried)}）"
    return VerifyResult(status="error", message=f"{message}：{detail}" if detail else message)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def quota_probe(data: dict) -> dict | None:
    """按凭据开关执行欠费探测，并复用运行时模型候选。

    模型列表可能包含账号不可用或无响应模型，因此逐个候选探测，获得结论后停止。
    """
    data = data or {}
    if not probe_requested(data):
        return None
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    api_key = (data.get("api_key") or "").strip()
    if not base_url or not api_key:
        return None
    for model in _candidates(data):
        verdict = quota_chat_probe(base_url, api_key, "openai", preferred_model=model)
        if verdict:
            return verdict
    # 候选耗尽仍无结论时返回未知状态，区分未启用探测与探测失败。
    return quota_unknown()


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。

    ``force=True`` 是用户点「刷新模型列表」——穿透 24h 缓存。
    """
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip(), force)


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
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
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
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
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
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
