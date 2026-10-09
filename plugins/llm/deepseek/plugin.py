"""DeepSeek API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

参考文档：https://api-docs.deepseek.com/

OpenAI 协议探针 + 官方余额接口 /user/balance。
"""
from __future__ import annotations

from typing import Optional

from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json
from llm_common import make_quota_probe, plausible_amount
from llm_protocols import probe_openai_models, chat_completion, fetch_models, chat_completion_stream, resolve_chat_model

NAME = "DeepSeek"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
PROTOCOL = "openai_models"

# 余额拿不到时的可选项（「设置」页开关，默认关闭）：发一次最小对话请求判断是否欠费
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")
# 余额端点（best-effort，仅认证通过后查询）
BALANCE_URL = "https://api.deepseek.com/user/balance"


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


def _parse_balance(payload: dict) -> Optional[dict]:
    """解析余额响应，返回 {"balance": float, "currency": str}。

    DeepSeek 的 balance_infos 可能同时含 CNY 与 USD 两项，且服务端返回顺序不稳定，
    故优先取 CNY，其次取首个可用项（避免币种随机）。
    """
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return None
    chosen = next(
        (i for i in infos if str(i.get("currency", "")).upper() == "CNY"),
        infos[0],
    )
    try:
        balance = float(chosen.get("total_balance", 0))
    except (TypeError, ValueError):
        return None
    # 金额异常（如中转实现返回的哨兵值）宁可不显示
    if not plausible_amount(balance):
        return None
    info = {"balance": balance, "currency": chosen.get("currency", "")}
    # 顶层 `is_available`：官方定义是「余额是否**够用**」（Whether the user's balance is
    # sufficient for API calls）——**不是**账号状态。它比余额数字更直接地回答「还能不能调」，
    # 由调用方据此判欠费（见 _verify）。取不到就不带这个键。
    if isinstance(payload.get("is_available"), bool):
        info["is_available"] = payload["is_available"]
    return info


def _fetch_balance(api_key: str) -> Optional[dict]:
    """查询余额（best-effort），失败返回 None。"""
    try:
        with make_client() as client:
            resp = client.get(BALANCE_URL, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            return None
        payload = safe_json(resp)
        if not isinstance(payload, dict):
            return None
        return _parse_balance(payload)
    except Exception:
        return None


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写 Base URL")

    result = probe_openai_models(base_url, api_key)

    # 认证成功：查询余额，负余额标记欠费
    if result.status == "valid":
        balance = _fetch_balance(api_key)
        if balance:
            info = dict(result.account_info or {})
            info["balance"] = balance["balance"]
            info["currency"] = balance["currency"]
            if balance.get("usage_rate") is not None:
                info["usage_rate"] = balance["usage_rate"]
            result.account_info = info
            # 官方 `is_available=false` = 「余额不足以继续调用」，比余额数字更权威，
            # 优先据此判欠费；没有这个字段时才退回看余额是否为负。
            if balance.get("is_available") is False:
                result.message = (
                    f"凭证有效，但账户余额不足（余额 {balance['balance']} {balance['currency']}）"
                )
            elif balance["balance"] < 0:
                # 欠费不算失效：凭据本身有效，仅账户余额不足
                result.message = (
                    f"凭证有效，但账户欠费（余额 {balance['balance']} {balance['currency']}）"
                )

    return result


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
# （模型不写死：运行时取 `/models` 清单首项。）官方 list-models 页现列 `deepseek-flash`（V4.1-Flash）
# 与 `deepseek-v4-pro`；旧的 `deepseek-chat` / `deepseek-reasoner` 已不再出现——思考与否现在是
# **同一个模型 + 参数**（`thinking={"type":"disabled"}` 或 `reasoning_effort:"none"`），不再靠换模型名区分。


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
