"""交易所类别共享模块：各服务验证处理器。

plugins/exchange/ 下的每个子插件（ht、okx、binance）都是独立插件，
通过 `from exchange_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持华泰证券（涨乐）、OKX、Binance 三类交易所/券商凭证。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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


# 服务清单：名称 + 是否需要券商密钥对（用于通用校验与展示）
SERVICE_CONFIGS = {
    "ht": {
        "name": "华泰证券（涨乐）",
        # 无公开联网校验端点，仅保存
        "verifiable": False,
    },
    "okx": {
        "name": "OKX",
        "verifiable": True,
    },
    "binance": {
        "name": "Binance",
        "verifiable": True,
    },
}


def _log(message: str) -> None:
    print(f"[exchange] {message}", file=sys.stderr)


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


def _mask(value: str) -> str:
    """仅展示密钥前缀，避免在列表/日志中回显完整凭据。"""
    value = (value or "").strip()
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}***{value[-4:]}"


# ── 各服务验证 ───────────────────────────────────────────

def _verify_ht(data: dict) -> VerifyResult:
    """华泰证券：暂无公开可用的联网校验接口。

    华泰涨乐（华泰证券）未开放面向个人开发者的公开行情/账户校验 API，
    无法在不接入其私有网关（需机构签约与专用 SDK）的情况下联网验证密钥。
    因此这里只做“凭证已保存”的占位结论，不做任何网络请求。
    """
    return VerifyResult(
        status="error",
        message="该券商暂无公开联网校验接口，仅保存",
    )


def _verify_okx(data: dict) -> VerifyResult:
    """OKX：GET /api/v5/account/balance，HMAC-SHA256 签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    passphrase = (data.get("passphrase") or "").strip()
    if not secret_key or not passphrase:
        return VerifyResult(status="invalid", message="缺少 Secret Key 或 Passphrase")

    path = "/api/v5/account/balance"
    request_path = path  # 无 query 参数，prehash 直接用路径
    # OKX 要求 ISO8601 毫秒精度、UTC、以 Z 结尾
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    # prehash = timestamp + method + requestPath + body（GET 无 body）
    prehash = timestamp + "GET" + request_path
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    url = "https://www.okx.com" + path
    with _client() as client:
        resp = client.get(url, headers=headers)

    payload = _safe_json(resp)
    code = str(payload.get("code", "")) if isinstance(payload, dict) else ""

    if code == "0":
        info: dict[str, Any] = {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            # 只取账户级汇总，不回显持仓/明细
            for key in ("totalEq", "adjEq", "isoEq", "uTime"):
                if rows[0].get(key) not in (None, ""):
                    info[key] = rows[0][key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # OKX 鉴权类错误码：密钥/口令/签名/权限不正确
    if code in ("50111", "50112", "50113", "50114"):
        detail = payload.get("msg") or payload.get("data") or ""
        message = f"凭证无效 (code {code})"
        if detail:
            message = f"{message}: {detail}"
        return VerifyResult(status="invalid", message=message)

    if resp.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")

    if code:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (code {code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(resp.status_code, _body_snippet(resp))


def _verify_binance(data: dict) -> VerifyResult:
    """Binance：GET /api/v3/account，HMAC-SHA256 查询串签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not secret_key:
        return VerifyResult(status="invalid", message="缺少 Secret Key")

    path = "/api/v3/account"
    # 参与签名的查询串必须与实际请求顺序一致
    timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    query_string = f"timestamp={timestamp}&recvWindow=5000"
    signature = hmac.new(
        secret_key.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{query_string}&signature={signature}"

    with _client() as client:
        resp = client.get(url, headers={"X-MBX-APIKEY": api_key})

    status = resp.status_code
    payload = _safe_json(resp)
    code = payload.get("code") if isinstance(payload, dict) else None

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            # 仅取非敏感账户能力字段
            for key in ("accountType", "canTrade", "canWithdraw", "canDeposit"):
                if payload.get(key) not in (None, ""):
                    info[key] = payload[key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 密钥无效 / 未启用现货 / 权限不足
    if code in (-2015, -1022, -1102):
        return VerifyResult(status="invalid", message=f"凭证无效 (code {code})")
    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    # 451：因地域限制拒绝访问，与凭证本身无关
    if status == 451:
        return VerifyResult(status="error", message="地域限制，无法从当前网络校验 (HTTP 451)")
    if code is not None:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (code {code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(status, _body_snippet(resp))


_SERVICE_VERIFIERS: dict[str, Callable[[dict], VerifyResult]] = {
    "ht": _verify_ht,
    "okx": _verify_okx,
    "binance": _verify_binance,
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

    # 需要密钥对（Secret / Passphrase）的服务补充校验
    if service == "okx":
        if not (data.get("secret_key") or "").strip():
            errors.append("Secret Key 不能为空")
        if not (data.get("passphrase") or "").strip():
            errors.append("Passphrase 不能为空")
    elif service == "binance":
        if not (data.get("secret_key") or "").strip():
            errors.append("Secret Key 不能为空")

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的凭证是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return VerifyResult(status="invalid", message="API Key 为空")

        verifier = _SERVICE_VERIFIERS.get(service)
        if verifier is None:
            return VerifyResult(status="error", message=f"未知服务: {service}")
        return verifier(data)

    except httpx.TimeoutException:
        return VerifyResult(status="error", message=f"请求超时（{int(TIMEOUT)} 秒）")
    except httpx.HTTPError as exc:
        _log(f"verify 网络异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"网络请求失败: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
        _log(f"verify 未预期异常: {type(exc).__name__}")
        return VerifyResult(status="error", message=f"插件执行异常: {type(exc).__name__}")


def display_summary(service: str, data: dict) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return name
    if not config.get("verifiable", True):
        return f"{name} | {api_key}（仅保存）"
    return f"{name} | {api_key}"


# 供子插件按需引用（保持与其他类别一致的显式导出）
__all__ = [
    "VerifyResult",
    "TIMEOUT",
    "SERVICE_CONFIGS",
    "validate_common",
    "verify_service",
    "display_summary",
]
