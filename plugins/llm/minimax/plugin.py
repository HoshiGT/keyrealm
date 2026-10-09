"""MiniMax API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://platform.minimax.cn/docs/api-reference/text-openai-api
参考文档：https://platform.minimax.io/docs/api-reference/text-openai-api
参考文档：https://platform.minimax.io/docs/api-reference/errorcode
"""
from __future__ import annotations

from urllib.parse import urlparse

from backend.plugin_sdk import VerifyResult, guarded, log
from llm_common import make_quota_probe
from llm_protocols import probe_openai_models, chat_completion, fetch_models, chat_completion_stream, resolve_chat_model

NAME = "MiniMax"
# 官方地区域名为国内 `api.minimax.cn` 和国际 `api.minimax.io`，Base URL 均包含 `/v1`。
# `api.minimax.chat` 未列于当前官方文档，不作为默认地址。
DEFAULT_BASE_URL = "https://api.minimax.cn/v1"
PROTOCOL = "openai_models"

# 官方已弃用 `max_tokens`，要求使用 `max_completion_tokens`。
TOKEN_PARAM = "max_completion_tokens"


def _base_url(data: dict) -> str:
    """归一化凭据的 Base URL，为裸域名补充 `/v1`。

    裸域名下 `GET /models` 会 308 重定向到 `/v1/models`，而 `POST /chat/completions` 返回 404。
    统一版本段可避免认证成功但对话失败。
    """
    raw = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if raw.rstrip("/") and not urlparse(raw).path.strip("/"):
        fixed = raw.rstrip("/") + "/v1"
        log(f"MiniMax：Base URL「{raw}」缺少版本段，已按官方约定补成「{fixed}」")
        return fixed
    return raw


# 余额拿不到时的可选项（「设置」页开关，默认关闭）：发一次最小对话请求判断是否欠费
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai", TOKEN_PARAM)


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
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    base_url = _base_url(data)
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip(), force)


# ── 可选操作：简单对话 ──────────────────────────────────
# 模型列表由 `/v1/models` 动态获取，避免依赖已不受支持的 `abab6.5s-chat`。


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
        token_param=TOKEN_PARAM,
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
        token_param=TOKEN_PARAM,
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
