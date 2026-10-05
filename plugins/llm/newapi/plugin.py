"""New API 中转站 API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

New API（one-api 的流行衍生版）自建中转站的专用校验：

- **有效性**：OpenAI 协议探针 ``GET /v1/models``，Bearer 认证；
- **余额**：one-api 兼容的 ``/dashboard/billing/subscription`` + ``/dashboard/billing/usage``。

New API 把硬限额 ``hard_limit_usd`` 定义成**令牌总额度（剩余 + 已用）**，
已用量走 usage 的 ``total_usage``（单位：美分），因此

    余额 = hard_limit_usd − total_usage / 100

**令牌为「不限额度」时服务端把硬限额填成 1e8 哨兵值**，相减会得出
99999996.47 这类假余额——这类哨兵值一律按「不限额度」处理，不显示数字。

Base URL 无默认值，必须由用户填写站点地址。
"""
from __future__ import annotations

from typing import Optional

import httpx

from llm_common import VerifyResult, guarded, make_client, make_quota_probe, plausible_amount, safe_json
from llm_protocols import probe_openai_models, chat_completion, fetch_models, chat_completion_stream

NAME = "New API 中转站"
DEFAULT_BASE_URL = ""
PROTOCOL = "openai_models"

# 余额拿不到时的可选项（「设置」页开关，默认关闭）：发一次最小对话请求判断是否欠费
quota_probe = make_quota_probe(DEFAULT_BASE_URL, "openai")


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    if not DEFAULT_BASE_URL and not (data.get("base_url") or "").strip():
        errors.append(f"厂商「{NAME}」需要填写站点地址")
    return errors


def _get_json(client: httpx.Client, url: str, headers: Optional[dict] = None) -> Optional[dict]:
    """GET 一个 JSON 端点；非 200 或非对象正文一律返回 None（best-effort）。"""
    resp = client.get(url, headers=headers or {})
    if resp.status_code != 200:
        return None
    return safe_json(resp)


def _first_amount(payload: dict) -> Optional[float]:
    """取第一个存在且可解析的金额字段，取不到返回 None。"""
    for key in ("hard_limit_usd", "system_hard_limit_usd", "soft_limit_usd"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _fetch_billing(base_url: str, api_key: str) -> Optional[dict]:
    """查询余额，返回 {"balance", "currency", "usage_rate"} 或 {"unlimited": True}；查不到返回 None。

    「不限额度」令牌返回 ``{"unlimited": True}``（无余额可言），由调用方决定怎么展示。
    """
    if not base_url:
        return None
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    with make_client() as client:
        sub = _get_json(client, f"{root}/dashboard/billing/subscription", headers)
        if sub is None:
            # 部分实现只在 /v1 前缀下挂载这套端点
            sub = _get_json(client, f"{root}/v1/dashboard/billing/subscription", headers)
        if not isinstance(sub, dict):
            return None

        total = _first_amount(sub)
        if total is None or total <= 0:
            return None
        # 哨兵值 = 不限额度令牌，余额无意义，且相减必得假值
        if not plausible_amount(total):
            return {"unlimited": True}

        usage = _get_json(client, f"{root}/dashboard/billing/usage", headers)
        if usage is None:
            usage = _get_json(client, f"{root}/v1/dashboard/billing/usage", headers)
        used = 0.0
        if isinstance(usage, dict):
            try:
                used = float(usage.get("total_usage", 0)) / 100.0  # 美分 → 美元
            except (TypeError, ValueError):
                used = 0.0

        balance = round(total - used, 6)
        if not plausible_amount(balance):
            return None
        result: dict = {"balance": balance, "currency": "USD"}
        result["usage_rate"] = max(0.0, min(1.0, used / total))
        return result


def _verify(data: dict) -> VerifyResult:
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")

    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return VerifyResult(status="error", message=f"厂商「{NAME}」需要填写站点地址")

    result = probe_openai_models(base_url, api_key)

    if result.status == "valid":
        info = dict(result.account_info or {})

        # 余额：不限额度令牌不显示数字，改用「套餐」一栏说明
        billing = _fetch_billing(base_url, api_key)
        if isinstance(billing, dict):
            if billing.get("unlimited"):
                info["plan"] = "不限额度"
            else:
                info["balance"] = billing["balance"]
                info["currency"] = billing["currency"]
                if billing.get("usage_rate") is not None:
                    info["usage_rate"] = billing["usage_rate"]
                if billing["balance"] < 0:
                    # 欠费不算失效：凭据本身有效，仅账户余额不足
                    result.message = (
                        f"凭证有效，但账户欠费（余额 {billing['balance']} {billing['currency']}）"
                    )

        result.account_info = info or None

    return result


@guarded
def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return _verify(data or {})


def list_models(data: dict) -> list:
    """获取该站点可用模型列表（供「简单对话」选择模型）。"""
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    return fetch_models("openai", base_url, (data.get("api_key") or "").strip())


# ── 可选操作：简单对话 ──────────────────────────────────
CHAT_MODEL = ""


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据含 API Key 时才提供简单对话）。"""
    if not (data or {}).get("api_key"):
        return []
    return [{"id": "chat", "name": "简单对话", "kind": "chat", "model": CHAT_MODEL}]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：简单对话。"""
    if action_id != "chat":
        return {"ok": False, "error": "未知操作"}
    data = data or {}
    base_url = (data.get("base_url") or "").strip() or DEFAULT_BASE_URL
    if not base_url:
        return {"ok": False, "error": "需要填写站点地址"}
    params = params or {}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        params.get("model") or CHAT_MODEL,
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
        return {"ok": False, "error": "需要填写站点地址"}
    return chat_completion_stream(
        base_url,
        (data.get("api_key") or "").strip(),
        params.get("model") or CHAT_MODEL,
        params.get("messages") or [],
        protocol="openai",
        on_delta=on_delta,
    )


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
