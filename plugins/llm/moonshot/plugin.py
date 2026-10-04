"""月之暗面 Moonshot API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

OpenAI 协议探针 + 官方余额接口 /v1/users/me/balance。
"""
from __future__ import annotations

from typing import Optional

from llm_common import VerifyResult, guarded, make_client, probe_openai_models, safe_json

NAME = "月之暗面 Moonshot"
DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
PROTOCOL = "openai_models"
# 余额端点（best-effort，仅认证通过后查询）
BALANCE_URL = "https://api.moonshot.cn/v1/users/me/balance"


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
    """解析余额响应，返回 {"balance": float, "currency": str}。"""
    data = payload.get("data")
    if isinstance(data, dict):
        try:
            return {
                "balance": float(data.get("available_balance", 0)),
                "currency": "CNY",
            }
        except (TypeError, ValueError):
            return None
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
            if balance["balance"] < 0:
                result.status = "invalid"
                result.message = f"账户欠费（余额 {balance['balance']} {balance['currency']}）"

    return result


def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    return guarded(lambda: _verify(data or {}))


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
