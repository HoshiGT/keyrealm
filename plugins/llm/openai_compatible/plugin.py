"""第三方中转 (OpenAI 兼容) API Key 验证插件（plugins/llm/ 类别下的独立子插件）。

Base URL 无默认值，必须由用户填写。
余额：one-api / new-api 类中转实现 /dashboard/billing/* 接口，best-effort 查询。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from llm_common import VerifyResult, guarded, make_client, safe_json
from llm_protocols import probe_openai_models, chat_completion, fetch_models

NAME = "第三方中转 (OpenAI 兼容)"
DEFAULT_BASE_URL = ""
PROTOCOL = "openai_models"
BALANCE_KIND = "openai_billing"


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


def _fetch_openai_billing(base_url: str, api_key: str) -> Optional[dict]:
    """OpenAI 兼容中转的余额：/dashboard/billing/subscription + /usage（one-api/new-api 实现）。

    仅在网关实现了这两个端点时可用，否则返回 None。
    """
    if not base_url:
        return None
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    with make_client() as client:
        sub = client.get(f"{root}/dashboard/billing/subscription", headers=headers)
        if sub.status_code != 200:
            return None
        sub_data = safe_json(sub)
        if not isinstance(sub_data, dict):
            return None
        try:
            total = float(sub_data.get("hard_limit_usd") or sub_data.get("system_hard_limit_usd") or 0)
        except (TypeError, ValueError):
            return None
        used = 0.0
        today = date.today()
        usage = client.get(
            f"{root}/dashboard/billing/usage",
            headers=headers,
            params={"start_date": today.replace(day=1).isoformat(), "end_date": today.isoformat()},
        )
        if usage.status_code == 200:
            u = safe_json(usage)
            if isinstance(u, dict):
                try:
                    used = float(u.get("total_usage", 0)) / 100.0  # 美分 → 美元
                except (TypeError, ValueError):
                    used = 0.0
    return {"balance": round(total - used, 6), "currency": "USD"}


def _fetch_balance(base_url: str, api_key: str) -> Optional[dict]:
    """查询余额（best-effort），失败返回 None。"""
    try:
        return _fetch_openai_billing(base_url, api_key)
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
        balance = _fetch_balance(base_url, api_key)
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


def list_models(data: dict) -> list:
    """获取该厂商可用模型列表（供「简单对话」选择模型）。"""
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
        return {"ok": False, "error": "需要填写 Base URL"}
    params = params or {}
    return chat_completion(
        base_url,
        (data.get("api_key") or "").strip(),
        params.get("model") or CHAT_MODEL,
        params.get("messages") or [],
        protocol="openai",
    )



def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    base_url = data.get("base_url") or DEFAULT_BASE_URL
    return f"{NAME} | {base_url}"
