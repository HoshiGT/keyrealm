"""商汤 SenseNova（日日新）API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

OpenAI 协议探针 ``GET {base}/models``。**没有公开的账户余额/用量接口**，所以欠费判定
交给可选的 ``quota_probe``（最小对话实测，见 PLUGIN_GUIDE §2.4）。

关于地址——商汤有两套互不相通的入口，模型名也不通用：

- ``https://token.sensenova.cn/v1``（**本插件默认值**）：SenseNova 平台的 OpenAI 兼容
  网关（Token Plan，按 5 小时滑动窗口计次而非计费）。实测 ``GET /v1/models`` 返回 200 +
  标准 ``{"data":[{"id":…}]}``，模型 id 形如 ``sensenova-6.8-flash-lite``。
  **该网关同时转挂三方模型**（实测列表里还有 ``deepseek-v4-flash`` / ``glm-5.2`` /
  ``kimi-k3`` 等），所以 ``list_models()`` 返回的未必都是商汤自家模型——只有 ``sensenova-*``
  前缀的是自家（**已无默认模型**，下拉首项未必是自家款，见「可选操作：简单对话」一节）。
- ``https://api.sensenova.cn/compatible-mode/v2``：大装置 ModelStudio，模型名形如
  ``SenseChat-5``；实测其 ``/compatible-mode/v1/models`` 返回 404，做不了模型探针。

用户若改用第二套地址，需同时把模型名换成 ``SenseChat-5`` 一类（对话框里的模型下拉
走的是 ``list_models()`` 实时列表，地址正确时会自动填上可用模型）。

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

# 余额拿不到（商汤无公开余额接口）时的可选项（凭据字段，默认关闭）：
# 发一次 max_tokens=1 的最小对话请求，实测账户额度是否真的还能用
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

    # 认证通过只能证明 Key 有效；Token Plan 的额度耗尽要靠 quota_probe 实测
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
# （模型不写死：运行时取 `/models` 清单首项。）注意该网关除商汤自家款外还转挂
# deepseek / glm / kimi 等三方模型，所以**清单首项未必是 `sensenova-*` 自家款**——
# 用户可在下拉里改选（`sensenova-6.8-flash-lite` 是自家轻量主力：便宜档、256K 上下文、多模态）。


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
