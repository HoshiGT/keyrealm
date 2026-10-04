"""API 服务类别共享模块：各服务验证处理器。

plugins/api/ 下的每个子插件（firecrawl、tavily、coze……）都是独立插件，
通过 `from api_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持 Firecrawl（网页抓取）、Tavily（搜索）、字节扣子 Coze（Agent 平台）。
本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import os
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
    print(f"[api] {message}", file=sys.stderr)


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


_SERVICE_VERIFIERS = {
    "firecrawl": _verify_firecrawl,
    "tavily": _verify_tavily,
    "coze": _verify_coze,
}


# ── 子插件入口（service 由子插件显式传入） ───────────────

def validate_common(service: str, data: dict) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]

    config = SERVICE_CONFIGS.get(service)
    if config is None:
        return [f"不支持的服务: {service}"]

    errors: list[str] = []

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        errors.append("API Key 不能为空")

    base_url = (data.get("base_url") or "").strip()
    if not config["default_base_url"] and not base_url:
        errors.append(f"服务「{config['name']}」需要填写 Base URL")

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的 API Key 是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="API Key 为空")

        base_url = (data.get("base_url") or "").strip() or config["default_base_url"]
        if not base_url:
            return VerifyResult(status="error", message=f"服务「{config['name']}」需要填写 Base URL")

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")
        return verifier(base_url, api_key)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        return VerifyResult(status="error", message=f"网络请求失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {exc!r}")
        return VerifyResult(status="error", message=f"插件执行异常: {exc}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)
    base_url = data.get("base_url") or config.get("default_base_url", "")
    return f"{name} | {base_url}"
