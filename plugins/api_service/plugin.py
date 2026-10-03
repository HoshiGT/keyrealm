"""第三方 API 服务 Key 验证插件。

支持 Firecrawl（网页抓取）、Tavily（搜索）、字节扣子 Coze（Agent 平台）。
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


# 请求超时（秒）
TIMEOUT = 20.0

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


SERVICE_CONFIGS = {
    "firecrawl": {
        "name": "Firecrawl",
        "default_base_url": "https://api.firecrawl.dev",
    },
    "tavily": {
        "name": "Tavily",
        "default_base_url": "https://api.tavily.com",
    },
    "coze": {
        "name": "字节扣子 Coze",
        "default_base_url": "https://api.coze.cn",
    },
}


def _log(message: str) -> None:
    print(f"[api_service] {message}", file=sys.stderr)


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


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


# ── 各服务验证 ───────────────────────────────────────────

def _verify_firecrawl(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/scrape"
    with _client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": "https://example.com"},
        )
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        if isinstance(payload, dict) and payload.get("success"):
            return VerifyResult(status="valid", message="凭证有效")
        return VerifyResult(status="error", message=f"响应异常: {_body_snippet(resp)}")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 402:
        return VerifyResult(status="invalid", message="账户欠费或无可用额度 (HTTP 402)")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_tavily(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/search"
    with _client() as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": "hello", "max_results": 1},
        )
    status = resp.status_code
    if status == 200:
        return VerifyResult(status="valid", message="凭证有效")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    if status == 429:
        return VerifyResult(status="valid", message="凭证有效，但触发速率限制 (HTTP 429)")
    return _unconfirmed(status, _body_snippet(resp))


def _verify_coze(base_url: str, api_key: str) -> VerifyResult:
    url = base_url.rstrip("/") + "/v1/workspaces"
    with _client() as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    status = resp.status_code
    payload = _safe_json(resp)
    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            total = payload["data"].get("total_count")
            if total is not None:
                info["workspace_count"] = total
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, _body_snippet(resp))


# ── 入口 ─────────────────────────────────────────────────

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
        errors.append("服务不能为空")
    elif vendor not in SERVICE_CONFIGS:
        errors.append(f"不支持的服务: {vendor}")
    else:
        base_url = (data.get("base_url") or "").strip()
        if not SERVICE_CONFIGS[vendor]["default_base_url"] and not base_url:
            errors.append(f"服务「{SERVICE_CONFIGS[vendor]['name']}」需要填写 Base URL")

    return errors


def verify(data: dict) -> VerifyResult:
    """验证 API Key 是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        vendor = data.get("vendor") or "firecrawl"
        config = SERVICE_CONFIGS.get(vendor)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {vendor}")

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="API Key 为空")

        base_url = (data.get("base_url") or "").strip() or config["default_base_url"]
        if not base_url:
            return VerifyResult(status="error", message=f"服务「{config['name']}」需要填写 Base URL")

        if vendor == "firecrawl":
            return _verify_firecrawl(base_url, api_key)
        if vendor == "tavily":
            return _verify_tavily(base_url, api_key)
        if vendor == "coze":
            return _verify_coze(base_url, api_key)
        return VerifyResult(status="error", message=f"未知服务: {vendor}")

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {exc!r}")
        return VerifyResult(status="error", message=f"插件执行异常: {exc}")


def get_display_summary(data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    vendor = data.get("vendor", "?")
    config = SERVICE_CONFIGS.get(vendor, {})
    name = config.get("name", vendor)
    base_url = data.get("base_url") or config.get("default_base_url", "")
    return f"{name} | {base_url}"
