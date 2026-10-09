"""华为云 ModelArts MaaS API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://support.huaweicloud.com/intl/zh-cn/model-call-maas/model-call-019.html  （MaaS 标准 API V2 · 对话）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-029.html             （获取模型列表）
参考文档：https://support.huaweicloud.com/model-call-maas/model-call-035.html             （错误码）
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import make_quota_probe
from llm_protocols import probe_openai_models, chat_completion, fetch_models, chat_completion_stream, resolve_chat_model

NAME = "华为云 ModelArts MaaS"
DEFAULT_BASE_URL = "https://api-ap-southeast-1.modelarts-maas.com/v2"
PROTOCOL = "openai_models"

# 余额拿不到时的可选项（「设置」页开关，默认关闭）：发一次最小对话请求判断是否欠费。
#
# 注意华为与商汤在 `max_tokens` 上的官方口径**相反**，而两边实测现象却一样（正文为空）：
#   华为官方：`max_tokens`「模型回答**不包含**思维链内容」——只限正文，思维链另算，
#             所以理论上 `max_tokens=1` 时正文该有内容；且 `max_tokens` 与
#             `max_completion_tokens` **不可同时设置**（同时给会直接报错）。
#   商汤官方：「Reasoning content and output **share** the `max_tokens` quota」——共用额度，
#             1 个 token 必然被思维链吃光。
# 华为这一侧**文档解释不了实测**（官方说该有正文、实测没有）。好在两边都不影响结论：
# `chat_completion` 只要「信封完整」就标记 `reached_model`，探测据此判「可用」——
# 请求确实到过模型并正常返回。真正的成因要抓原始响应体（错误信息里已带响应体摘要）。
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")


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

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
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
