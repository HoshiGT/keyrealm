"""LLM 类别共享模块：协议处理器与厂商配置。

plugins/llm/ 下的每个子插件（openai、deepseek……）都是独立插件，
通过 `from llm_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持 OpenAI、Anthropic、DeepSeek、OpenRouter、Gemini、MiniMax、NVIDIA NIM、
Moonshot、硅基流动、阿里百炼、华为 MaaS、智谱 GLM、字节豆包、阿里通义、
百度文心，以及各类 OpenAI/Anthropic 兼容中转。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import httpx


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


# 请求超时（秒）
TIMEOUT = 20.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240

# 欠费/余额不足判定关键字（出现在错误正文中）
_INSUFFICIENT_BALANCE_KEYWORDS = (
    "余额不足",
    "充值",
    "insufficient",
    "no balance",
    "out of balance",
    "1113",  # 智谱错误码：余额不足
)


# ── 厂商配置：协议 + 默认地址 + 可选余额端点 ──────────────
#
# protocol 取值：
#   openai_models       GET {base}/models，Bearer
#   anthropic_models    GET {base}/models，x-api-key + anthropic-version
#   openai_chat         POST {base}/chat/completions（识别欠费，消耗极小额度）
#   anthropic_chat      POST {base}/v1/messages（识别欠费）
#   openrouter          GET {base}/auth/key（返回 key 信息与用量）
#   gemini              GET {base}/models?key=
#   wenxin              POST {base}/oauth/2.0/token
VENDOR_CONFIGS = {
    "openai": {
        "name": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "protocol": "openai_models",
    },
    "anthropic": {
        "name": "Anthropic",
        "default_base_url": "https://api.anthropic.com/v1",
        "protocol": "anthropic_models",
    },
    "deepseek": {
        "name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/v1",
        "protocol": "openai_models",
        "balance_url": "https://api.deepseek.com/user/balance",
        "balance_kind": "deepseek",
    },
    "openrouter": {
        "name": "OpenRouter",
        "default_base_url": "https://openrouter.ai/api/v1",
        "protocol": "openrouter",
        "balance_url": "https://openrouter.ai/api/v1/credits",
        "balance_kind": "openrouter",
    },
    "gemini": {
        "name": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "protocol": "gemini",
    },
    "minimax": {
        "name": "MiniMax",
        "default_base_url": "https://api.minimax.chat/v1",
        "protocol": "openai_models",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "default_base_url": "https://integrate.api.nvidia.com/v1",
        "protocol": "openai_chat",
        "chat_model": "deepseek-ai/deepseek-v4.1-flash",
    },
    "moonshot": {
        "name": "月之暗面 Moonshot",
        "default_base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai_models",
        "balance_url": "https://api.moonshot.cn/v1/users/me/balance",
        "balance_kind": "moonshot",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "protocol": "openai_models",
    },
    "dashscope": {
        "name": "阿里云百炼 DashScope",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocol": "openai_models",
    },
    "huawei_maas": {
        "name": "华为云 ModelArts MaaS",
        "default_base_url": "https://api-ap-southeast-1.modelarts-maas.com/v2",
        "protocol": "openai_models",
    },
    "glm": {
        "name": "智谱 GLM",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai_chat",
        "chat_model": "glm-4.6",
    },
    "glm_anthropic": {
        "name": "智谱 GLM (Anthropic)",
        "default_base_url": "https://open.bigmodel.cn/api/anthropic",
        "protocol": "anthropic_chat",
        "chat_model": "claude-sonnet-4",
    },
    "doubao": {
        "name": "字节豆包",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "protocol": "openai_models",
    },
    "tongyi": {
        "name": "阿里通义",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocol": "openai_models",
    },
    "wenxin": {
        "name": "百度文心",
        "default_base_url": "https://aip.baidubce.com",
        "protocol": "wenxin",
    },
    "openai_compatible": {
        "name": "第三方中转 (OpenAI 兼容)",
        "default_base_url": "",
        "protocol": "openai_models",
        # one-api / new-api 类中转实现 /dashboard/billing/* 余额接口，best-effort 查询
        "balance_kind": "openai_billing",
    },
    "anthropic_compatible": {
        "name": "第三方中转 (Anthropic 协议)",
        "default_base_url": "",
        "protocol": "anthropic_models",
        "verify_path": "/v1/models",
    },
}


def _log(message: str) -> None:
    print(f"[llm] {message}", file=sys.stderr)


def _split_wenxin_key(api_key: str) -> tuple[str, str]:
    """百度文心凭证格式为 API_KEY|SECRET_KEY。"""
    parts = api_key.split("|", 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


# ── 请求封装 ─────────────────────────────────────────────

def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def _proxy() -> Optional[str]:
    """验证代理：优先 VAULT_PROXY（应用设置写入），其次标准代理环境变量。"""
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, proxy=_proxy())


# ── 协议处理器 ───────────────────────────────────────────

def _verify_openai_models(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/models"
    with _client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    return _interpret_simple(resp)


def _verify_anthropic_models(base_url: str, api_key: str, verify_path: str = "/models") -> VerifyResult:
    url = base_url.rstrip("/") + verify_path
    with _client() as client:
        resp = client.get(
            url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    return _interpret_simple(resp)


def _verify_openai_chat(base_url: str, api_key: str, model: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    with _client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
    return _interpret_chat(resp)


def _verify_anthropic_chat(base_url: str, api_key: str, model: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/messages"
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    with _client() as client:
        resp = client.post(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
        )
    return _interpret_chat(resp)


def _verify_openrouter(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/auth/key"
    with _client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        # 单 key 限额：有 limit 时给出剩余额度与使用率（plan 类账号）
        info: dict[str, Any] = {}
        d = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(d, dict):
            limit, usage, remaining = d.get("limit"), d.get("usage"), d.get("limit_remaining")
            if isinstance(remaining, (int, float)):
                info["balance"] = remaining
                info["currency"] = "USD"
            if isinstance(limit, (int, float)) and isinstance(usage, (int, float)) and limit > 0:
                info["usage_rate"] = max(0.0, min(1.0, usage / limit))
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_gemini(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/models"
    with _client() as client:
        resp = client.get(url, params={"key": api_key})
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    # Google 无效 key 返回 400（INVALID_ARGUMENT）或 403
    if status in (400, 401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_wenxin(base_url: str, api_key: str) -> VerifyResult:
    client_id, client_secret = _split_wenxin_key(api_key)
    url = base_url.rstrip("/") + "/oauth/2.0/token"
    with _client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    status = resp.status_code
    payload = _safe_json(resp)
    if isinstance(payload, dict):
        if payload.get("access_token"):
            info: dict[str, Any] = {}
            if payload.get("expires_in") is not None:
                info["expires_in"] = payload["expires_in"]
            return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
        if payload.get("error"):
            detail = payload.get("error_description") or payload.get("error")
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


# ── 响应判定 ─────────────────────────────────────────────

def _interpret_simple(resp: httpx.Response) -> VerifyResult:
    """简单 GET 探针（openai_models / anthropic_models）的通用判定。"""
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, _body_snippet(resp))


def _interpret_chat(resp: httpx.Response) -> VerifyResult:
    """chat 探针判定：额外识别 429 余额不足（欠费）与速率限制。"""
    status = resp.status_code
    payload = _safe_json(resp)
    snippet = _body_snippet(resp)
    if status == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        if _looks_insufficient_balance(snippet):
            return VerifyResult(status="invalid", message="账户欠费或无可用资源包")
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    if status == 402:
        return VerifyResult(status="invalid", message="账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, snippet)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _looks_insufficient_balance(snippet: str) -> bool:
    text = (snippet or "").lower()
    return any(kw in text for kw in _INSUFFICIENT_BALANCE_KEYWORDS)


# ── 账号信息提取 ─────────────────────────────────────────

def _extract_account_info(payload: Any) -> Optional[dict]:
    """从响应中提取账号信息。

    LLM 的「附加信息」只展示余额 / 使用率，故不再回传模型数量等噪声字段；
    余额由 verify_vendor 统一通过各厂商的 balance_url 查询。
    """
    if not isinstance(payload, dict):
        return None

    info: dict[str, Any] = {}
    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]

    return info or None


def _parse_balance(kind: str, payload: dict) -> Optional[dict]:
    """解析余额响应，返回 {"balance": float, "currency": str}。"""
    if kind == "deepseek":
        infos = payload.get("balance_infos")
        if isinstance(infos, list) and infos:
            first = infos[0]
            try:
                return {
                    "balance": float(first.get("total_balance", 0)),
                    "currency": first.get("currency", ""),
                }
            except (TypeError, ValueError):
                return None
    elif kind == "moonshot":
        data = payload.get("data")
        if isinstance(data, dict):
            try:
                return {
                    "balance": float(data.get("available_balance", 0)),
                    "currency": "CNY",
                }
            except (TypeError, ValueError):
                return None
    elif kind == "siliconflow":
        data = payload.get("data")
        if isinstance(data, dict):
            raw = data.get("totalBalance", data.get("balance"))
            try:
                return {"balance": float(raw), "currency": "CNY"}
            except (TypeError, ValueError):
                return None
    elif kind == "openrouter":
        data = payload.get("data")
        if isinstance(data, dict):
            try:
                total = float(data.get("total_credits", 0))
                used = float(data.get("total_usage", 0))
            except (TypeError, ValueError):
                return None
            if total <= 0:
                return None  # 免费额度账号无余额概念
            result: dict[str, Any] = {"balance": round(total - used, 6), "currency": "USD"}
            result["usage_rate"] = max(0.0, min(1.0, used / total))
            return result
    return None


def _fetch_openai_billing(client: httpx.Client, base_url: str, api_key: str) -> Optional[dict]:
    """OpenAI 兼容中转的余额：/dashboard/billing/subscription + /usage（one-api/new-api 实现）。

    仅在网关实现了这两个端点时可用，否则返回 None。
    """
    if not base_url:
        return None
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    sub = client.get(f"{root}/dashboard/billing/subscription", headers=headers)
    if sub.status_code != 200:
        return None
    sub_data = _safe_json(sub)
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
        u = _safe_json(usage)
        if isinstance(u, dict):
            try:
                used = float(u.get("total_usage", 0)) / 100.0  # 美分 → 美元
            except (TypeError, ValueError):
                used = 0.0
    return {"balance": round(total - used, 6), "currency": "USD"}


def _fetch_balance(config: dict, api_key: str, base_url: str = "") -> Optional[dict]:
    """查询余额（best-effort），失败返回 None。"""
    kind = config.get("balance_kind", "")
    try:
        with _client() as client:
            if kind == "openai_billing":
                return _fetch_openai_billing(client, base_url, api_key)
            balance_url = config.get("balance_url")
            if not balance_url:
                return None
            resp = client.get(balance_url, headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code != 200:
                return None
            payload = _safe_json(resp)
            if not isinstance(payload, dict):
                return None
            return _parse_balance(kind, payload)
    except Exception:
        return None


# ── 子插件入口（vendor 由子插件显式传入） ────────────────

def _dispatch(protocol: str, config: dict, base_url: str, api_key: str) -> VerifyResult:
    if protocol == "openai_models":
        return _verify_openai_models(base_url, api_key)
    if protocol == "anthropic_models":
        return _verify_anthropic_models(base_url, api_key, config.get("verify_path", "/models"))
    if protocol == "openai_chat":
        return _verify_openai_chat(base_url, api_key, config.get("chat_model", ""))
    if protocol == "anthropic_chat":
        return _verify_anthropic_chat(base_url, api_key, config.get("chat_model", ""))
    if protocol == "openrouter":
        return _verify_openrouter(base_url, api_key)
    if protocol == "gemini":
        return _verify_gemini(base_url, api_key)
    if protocol == "wenxin":
        return _verify_wenxin(base_url, api_key)
    return VerifyResult(status="error", message=f"未知协议: {protocol}")


def validate_common(vendor: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = VENDOR_CONFIGS.get(vendor)
    if config is None:
        return [f"不支持的厂商: {vendor}"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if not config["default_base_url"] and not base_url:
        errors.append(f"厂商「{config['name']}」需要填写 Base URL")
    if vendor == "wenxin" and api_key:
        client_id, client_secret = _split_wenxin_key(api_key)
        if not client_id or not client_secret:
            errors.append("百度文心凭证格式应为 API_KEY|SECRET_KEY")

    return errors


def verify_vendor(vendor: str, data: dict) -> VerifyResult:
    """验证指定厂商的 API Key 是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = VENDOR_CONFIGS.get(vendor)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的厂商: {vendor}")

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="API Key 为空")

        base_url = (data.get("base_url") or "").strip() or config["default_base_url"]
        if not base_url:
            return VerifyResult(
                status="error",
                message=f"厂商「{config['name']}」需要填写 Base URL",
            )

        result = _dispatch(config["protocol"], config, base_url, api_key)

        # 认证成功：查询余额（各厂商 balance_url / 中转 billing 接口），负余额标记欠费
        if result.status == "valid" and (config.get("balance_url") or config.get("balance_kind")):
            balance = _fetch_balance(config, api_key, base_url)
            if balance:
                info = dict(result.account_info or {})
                info["balance"] = balance["balance"]
                info["currency"] = balance["currency"]
                if balance.get("usage_rate") is not None:
                    info["usage_rate"] = balance["usage_rate"]
                result.account_info = info
                if balance["balance"] < 0:
                    result.status = "invalid"
                    result.message = (
                        f"账户欠费（余额 {balance['balance']} {balance['currency']}）"
                    )

        return result

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {exc!r}")
        return VerifyResult(status="error", message=f"插件执行异常: {exc}")


def display_summary(vendor: str, data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    config = VENDOR_CONFIGS.get(vendor, {})
    name = config.get("name", vendor)
    base_url = data.get("base_url") or config.get("default_base_url", "")
    return f"{name} | {base_url}"
