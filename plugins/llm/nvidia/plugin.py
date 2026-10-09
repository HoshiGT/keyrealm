"""NVIDIA NIM API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

**鉴权只能靠 chat 端点**：`GET /v1/models` 不鉴权（匿名与真 key 返回同一份清单，
拿它验证会「任何 key 都有效」）。`POST /v1/chat/completions` 才走鉴权
（坏 key → 401/403，真 key → 200，欠费 → 402/429）。

但 NVIDIA **会下线模型**，且各账号开通的模型不同（清单是过期超集：大量 410 Gone
与 404 Not found for account），所以**不能赌单个模型名**——候选逐个短超时试，命中即止
（历史上的硬编码 model 就曾因「网关接受后永不响应」把真 key 必卡成超时）。

候选名单**不再写死**：运行时从 ``/v1/models`` 按「像便宜对话模型」的线索现挑，
结果按 24h 缓存（见 ``llm_protocols`` 的模型清单缓存说明）；用户点「刷新模型列表」即穿透缓存。

> **哪些是实测、哪些有官方背书**（2026-10-07 逐页核对官方文档后的结论）：
> 上面三点——「`/v1/models` 不鉴权」「清单是过期超集、下线模型返 410/404」「欠费 → 402/429」
> ——**官方文档均未背书**。官方 API 参考只声明 `200/202/422/500` 与通用 `InvokeError` 错误体，
> `401/402/403/410/429` **一字未提**；模型清单「过期超集」与「410 Gone」只见于开发者论坛，
> 官方参考页的 `404` 指的是**本地容器启动/端口**问题，与模型下线无关。
> 所以这些判断**别拿官方文档去"纠正"**，它们来自本机实测。
>
> 另：`reasoning_content` **不在**各模型 OpenAPI 的 `Message` schema 里（`additionalProperties:false`），
> 但**同一页的官方示例代码又去读它**（`getattr(chunk.choices[0].delta, "reasoning_content", None)`）
> ——官方自己就用容错取值。所以我们按「字段可能不存在」处理是对的，别改成硬读。

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

# 单个候选给的超时：多个候选累加也不能太久。卡住的模型会烧满这个值再换下一个。
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
    """探针候选：凭据指定的 ``probe_model`` 优先，其后是**运行时**从 ``/v1/models`` 挑的候选。

    名单来自 ``probe_model_candidates``（按 24h 缓存，用户刷新即穿透）——**不再内置写死短名单**。
    NVIDIA 的清单是过期超集，所以仍然靠「多个候选 + 短超时 + 换下一个」而不是押注某一个名字。
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
        # 200 → 有效；401/403 → 密钥本身无效，换模型也是白换，直接定论
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
    """欠费探测（凭据开关 ``probe_chat``，默认关）：复用 verify 那套**运行时候选**。

    通用实现 ``make_quota_probe`` 只会「自动挑一个名字像 flash 的便宜模型」然后**只试它一个**；
    NVIDIA 的清单是过期超集，单一模型很容易挑到已失效/挂死的款，所以这里**逐个候选试**、
    命中即止（候选来源与 verify 相同：``probe_model_candidates``，带 24h 缓存）。
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
    # 候选都试完仍无结论：报「实测 异常」，别静默——勾了开关却什么都不显示，用户分不清
    # 「没开探测」和「探测失败」。
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
