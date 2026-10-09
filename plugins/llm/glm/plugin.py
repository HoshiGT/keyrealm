"""智谱 GLM API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://docs.bigmodel.cn/api-reference

智谱**不按协议分权**：同一把 key 既能调 OpenAI 兼容端点，也能调 Anthropic 兼容端点
（2026-10-05 实测三把 key 一致；`claude-sonnet-4` 这类 Claude 模型名也由智谱做了映射）。
因此插件只有一条路径——**OpenAI 兼容端点**，不设「接口格式」开关，也不为旧地址做特判：

- 普通平台：`/api/paas/v4`（默认）
- GLM Coding Plan：`/api/coding/paas/v4`（按需在 Base URL 里填）

GLM 有 `/models`（2026-10-09 实测：无鉴权访问返回 401 → 端点存在），但 **chat 探针**能额外识别
429 里的余额不足错误码 **1113**，所以 verify 优先走 chat 探针、拿不到模型时退回 `/models` 探针。
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import make_quota_probe
from llm_protocols import (
    chat_completion,
    chat_completion_stream,
    fetch_models,
    probe_openai_chat,
    probe_openai_models,
    resolve_chat_model,
)

NAME = "智谱 GLM"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
PROTOCOL = "openai_chat"

# 余额拿不到时的可选项（凭据级开关，默认关闭）：发一次最小对话请求判断是否欠费
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")


def base_url_of(data: dict) -> str:
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

    base_url = base_url_of(data)
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    # 探测模型**绝不写死**：优先凭据自填的 `probe_model`，其次取运行时 `/models` 清单首项。
    model = resolve_chat_model(data.get("probe_model"), lambda: list_models(data))
    if not model:
        # 拿不到模型（多半是 key 无效导致 /models 401）→ 退回**不需要模型**的 `/models` 探针。
        # 否则「坏 key」会被误报成「无法确定探测模型」，**假红比漏报更坑**。
        return probe_openai_models(base_url, api_key)
    return probe_openai_chat(base_url, api_key, model)


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict, force: bool = False) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    return fetch_models("openai", base_url_of(data), (data.get("api_key") or "").strip(), force)


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
    base_url = base_url_of(data)
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
    base_url = base_url_of(data)
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
    return f"{NAME} | {base_url_of(data or {})}"
