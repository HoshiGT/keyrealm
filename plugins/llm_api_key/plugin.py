"""LLM API Key 验证插件。

支持 OpenAI、Anthropic、字节豆包、阿里通义、百度文心。
插件完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
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


VENDOR_CONFIGS = {
    "openai": {
        "default_base_url": "https://api.openai.com/v1",
        "verify_path": "/models",
        "method": "GET",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "anthropic": {
        "default_base_url": "https://api.anthropic.com/v1",
        "verify_path": "/models",
        "method": "GET",
        "headers": lambda key: {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    },
    "doubao": {
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "verify_path": "/models",
        "method": "GET",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "tongyi": {
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "verify_path": "/models",
        "method": "GET",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "wenxin": {
        "default_base_url": "https://aip.baidubce.com",
        "verify_path": "/oauth/2.0/token",
        "method": "POST",
        "headers": lambda key: {"Content-Type": "application/x-www-form-urlencoded"},
        "params": lambda key: {
            "grant_type": "client_credentials",
            "client_id": key.split("|")[0] if "|" in key else key,
            "client_secret": key.split("|")[1] if "|" in key else "",
        },
    },
}

# 请求超时（秒）
TIMEOUT = 10.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 200


def _log(message: str) -> None:
    print(f"[llm_api_key] {message}", file=sys.stderr)


def _split_wenxin_key(api_key: str) -> tuple[str, str]:
    """百度文心凭证格式为 API_KEY|SECRET_KEY。"""
    parts = api_key.split("|", 1)
    api_key_part = parts[0].strip()
    secret_part = parts[1].strip() if len(parts) > 1 else ""
    return api_key_part, secret_part


def validate(data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    vendor = data.get("vendor")
    if not vendor:
        errors.append("厂商不能为空")
    elif vendor not in VENDOR_CONFIGS:
        errors.append(f"不支持的厂商: {vendor}")
    elif vendor == "wenxin" and api_key:
        client_id, client_secret = _split_wenxin_key(api_key)
        if not client_id or not client_secret:
            errors.append("百度文心凭证格式应为 API_KEY|SECRET_KEY")

    return errors


def verify(data: dict) -> VerifyResult:
    """验证 LLM API Key 是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        vendor = data.get("vendor") or "openai"
        config = VENDOR_CONFIGS.get(vendor)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的厂商: {vendor}")

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="API Key 为空")

        base_url = (data.get("base_url") or "").strip() or config["default_base_url"]
        url = base_url.rstrip("/") + config["verify_path"]
        method = config["method"]

        headers = config["headers"](api_key)
        params = config["params"](api_key) if config.get("params") else None

        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            if method == "POST":
                response = client.post(url, headers=headers, data=params)
            else:
                response = client.get(url, headers=headers)

        return _interpret(vendor, response)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message="请求超时（10 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {exc!r}")
        return VerifyResult(status="error", message=f"插件执行异常: {exc}")


def _interpret(vendor: str, response: httpx.Response) -> VerifyResult:
    """根据厂商与响应判定验证结果。"""
    status_code = response.status_code
    payload = _safe_json(response)
    snippet = _body_snippet(response)

    if vendor == "wenxin":
        return _interpret_wenxin(status_code, payload, snippet)

    if status_code == 200:
        return VerifyResult(
            status="valid",
            message="凭证有效",
            account_info=_extract_account_info(payload),
        )
    if status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status_code})")
    if status_code == 429:
        return VerifyResult(status="valid", message="凭证有效，但已触发速率限制 (HTTP 429)")

    message = f"无法确认凭证状态 (HTTP {status_code})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _interpret_wenxin(
    status_code: int, payload: Optional[dict], snippet: str
) -> VerifyResult:
    """百度文心通过 access_token 是否返回判定，成功与失败均为 HTTP 200。"""
    if isinstance(payload, dict):
        if payload.get("access_token"):
            info: dict[str, Any] = {}
            if payload.get("expires_in") is not None:
                info["expires_in"] = payload["expires_in"]
            return VerifyResult(
                status="valid",
                message="凭证有效",
                account_info=info or None,
            )
        if payload.get("error"):
            detail = payload.get("error_description") or payload.get("error")
            return VerifyResult(status="invalid", message=f"凭证无效: {detail}")

    if status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status_code})")

    message = f"无法确认凭证状态 (HTTP {status_code})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _extract_account_info(payload: Any) -> Optional[dict]:
    """从 /models 响应中尽力提取账号相关信息。"""
    if not isinstance(payload, dict):
        return None

    info: dict[str, Any] = {}

    models = payload.get("data")
    if isinstance(models, list):
        info["model_count"] = len(models)

    if isinstance(payload.get("object"), str):
        info["object"] = payload["object"]
    if isinstance(payload.get("first_id"), str):
        info["first_id"] = payload["first_id"]
    if isinstance(payload.get("last_id"), str):
        info["last_id"] = payload["last_id"]
    if payload.get("has_more") is not None:
        info["has_more"] = bool(payload["has_more"])

    for key in ("organization", "org_id"):
        if payload.get(key):
            info[key] = payload[key]

    return info or None


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


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    vendor = data.get("vendor", "?")
    base_url = data.get("base_url") or VENDOR_CONFIGS.get(vendor, {}).get(
        "default_base_url", ""
    )
    return f"{vendor} | {base_url}"