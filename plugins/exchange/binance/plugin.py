"""Binance API 凭证验证插件（plugins/exchange/ 类别下的独立子插件）。

Binance：GET /api/v3/account，HMAC-SHA256 查询串签名鉴权。
签名算法与判定逻辑随插件自带，不依赖 exchange_common。
"""
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Optional

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "Binance"


def _mask(value: str) -> str:
    """仅展示密钥前缀，避免在列表/日志中回显完整凭据。"""
    value = (value or "").strip()
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}***{value[-4:]}"


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not (data.get("api_key") or "").strip():
        errors.append("API Key 不能为空")
    if not (data.get("secret_key") or "").strip():
        errors.append("Secret Key 不能为空")
    return errors


def _binance_server_time() -> Optional[int]:
    """取 Binance 服务器时间（毫秒），避免本机时钟偏差触发 -1021；失败返回 None。"""
    try:
        with make_client() as client:
            resp = client.get("https://api.binance.com/api/v3/time")
        payload = safe_json(resp)
        if isinstance(payload, dict) and isinstance(payload.get("serverTime"), int):
            return payload["serverTime"]
    except Exception:  # noqa: BLE001 - 拿不到就回退本机时间
        return None
    return None


@guarded
def verify(data) -> VerifyResult:
    """Binance：GET /api/v3/account，HMAC-SHA256 查询串签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key:
        return VerifyResult(status="invalid", message="缺少 Secret Key")

    path = "/api/v3/account"
    # 参与签名的查询串必须与实际请求顺序一致；优先用服务器时间规避时钟偏差(-1021)
    timestamp = _binance_server_time() or int(datetime.now(timezone.utc).timestamp() * 1000)
    query_string = f"timestamp={timestamp}&recvWindow=5000"
    signature = hmac.new(
        secret_key.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{query_string}&signature={signature}"

    with make_client() as client:
        resp = client.get(url, headers={"X-MBX-APIKEY": api_key})

    status = resp.status_code
    payload = safe_json(resp)
    code = payload.get("code") if isinstance(payload, dict) else None

    if status == 200:
        info: dict[str, Any] = {}
        if isinstance(payload, dict):
            # 仅取非敏感账户能力字段
            for key in ("accountType", "canTrade", "canWithdraw", "canDeposit"):
                if payload.get(key) not in (None, ""):
                    info[key] = payload[key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 密钥无效 / 未启用现货 / 权限不足 / IP 未白名单（-2015 三者皆可能，附服务端原因）
    if code in (-2015, -1022, -1102):
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(status="invalid", message=f"凭证无效 (code {code})" + (f": {detail}" if detail else ""))
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
    return _unconfirmed(status, body_snippet(resp))


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
