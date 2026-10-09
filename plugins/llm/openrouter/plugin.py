"""OpenRouter API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

协议：GET /auth/key（返回 key 信息与用量，plan 类账号带 limit/usage）
余额：GET /credits（total_credits - total_usage → 余额与使用率）

参考文档：https://openrouter.ai/docs/quickstart
"""
from __future__ import annotations

from typing import Any, Optional

from backend.plugin_sdk import VerifyResult, body_snippet, guarded, make_client, safe_json
from llm_common import make_quota_probe, plausible_amount
from llm_protocols import chat_completion, fetch_models, chat_completion_stream, resolve_chat_model

NAME = "OpenRouter"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
PROTOCOL = "openrouter"

# 余额不可用时的可选校验（设置页开关，默认关闭）：发送最小对话请求判断账户状态
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")
AUTH_KEY_PATH = "/auth/key"
# 余额端点（best-effort，仅认证通过后查询）
BALANCE_URL = "https://openrouter.ai/api/v1/credits"


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


def _probe_auth_key(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + AUTH_KEY_PATH
    with make_client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    status = resp.status_code
    payload = safe_json(resp)
    if status == 200:
        # 单 key 限额：有 limit 时给出剩余额度与使用率（plan 类账号）
        info: dict[str, Any] = {}
        d = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(d, dict):
            limit, usage, remaining = d.get("limit"), d.get("usage"), d.get("limit_remaining")
            if isinstance(remaining, (int, float)):
                info["balance"] = remaining
                info["currency"] = "USD"
            elif d.get("limit") is None:
                # `limit` 为 null 表示该 key 不限额；记录为「不限额度」，以便在附加信息中明确展示。
                info["plan"] = "不限额度"
            if isinstance(limit, (int, float)) and isinstance(usage, (int, float)) and limit > 0:
                info["usage_rate"] = max(0.0, min(1.0, usage / limit))
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    message = f"无法确认凭证状态 (HTTP {status})"
    snippet = body_snippet(resp)
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _parse_balance(payload: dict) -> Optional[dict]:
    """解析 /credits 响应，返回 {"balance": float, "currency": str, "usage_rate": float}。"""
    data = payload.get("data")
    if isinstance(data, dict):
        try:
            total = float(data.get("total_credits", 0))
            used = float(data.get("total_usage", 0))
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None  # 免费额度账号无余额概念
        # 金额异常（如中转实现返回的哨兵值）宁可不显示
        if not plausible_amount(total):
            return None
        balance = round(total - used, 6)
        if not plausible_amount(balance):
            return None
        result: dict[str, Any] = {"balance": balance, "currency": "USD"}
        result["usage_rate"] = max(0.0, min(1.0, used / total))
        return result
    return None


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

    result = _probe_auth_key(base_url, api_key)

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
            if balance["balance"] < 0:
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
