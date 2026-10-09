"""商汤 SenseNova（日日新）API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

通过 OpenAI 协议探针 ``GET {base}/models`` 验证凭据。该平台没有公开的账户余额/用量接口，
欠费判定由可选的 ``quota_probe`` 执行最小对话请求（见 PLUGIN_GUIDE §2.4）。

平台存在两套互不相通的入口，模型名也不通用：

- ``https://token.sensenova.cn/v1``（**本插件默认值**）：SenseNova 平台的 OpenAI 兼容网关，
  按 5 小时滑动窗口计次的 Token Plan。``GET /v1/models`` 返回 HTTP 200 的标准模型列表，模型 ID 形如
  ``sensenova-6.8-flash-lite``；网关也提供第三方模型，因此 ``list_models()`` 返回的模型不一定
  属于商汤，且不预设默认模型。
- ``https://api.sensenova.cn/compatible-mode/v2``：大装置 ModelStudio，模型名形如
  ``SenseChat-5``；其 ``/compatible-mode/v1/models`` 返回 HTTP 404，不提供可用的模型探针。

使用第二套地址时，模型名也必须切换为 ``SenseChat-5`` 一类。对话框模型下拉列表来自
``list_models()`` 的实时结果。

参考文档：https://platform.sensenova.cn/zh/docs               （默认网关 token.sensenova.cn 的官方文档）
参考文档：https://www.sensecore.cn/help/docs/model-as-a-service/nova/overview/compatible-mode  （大装置 ModelStudio 那套）
"""
from __future__ import annotations

from backend.plugin_sdk import VerifyResult, guarded
from llm_common import make_quota_probe
from llm_protocols import chat_completion, chat_completion_stream, fetch_models, probe_openai_models, resolve_chat_model

NAME = "商汤 SenseNova"
DEFAULT_BASE_URL = "https://token.sensenova.cn/v1"
PROTOCOL = "openai"

# 余额接口不可用时的可选项（凭据字段，默认关闭）：发送 max_tokens=1 的最小对话请求，
# 判断账户额度是否仍可用。
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

    # 认证通过只能证明 Key 有效；Token Plan 的额度耗尽需要 quota_probe 查询。
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
# 模型不写死：运行时读取 `/models` 清单。该网关同时提供第三方模型，清单首项不保证
# 属于 `sensenova-*` 前缀；用户可从下拉列表选择。


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
