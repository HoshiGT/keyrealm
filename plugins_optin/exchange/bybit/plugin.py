"""Bybit API 凭证验证插件（plugins_optin/exchange/bybit/，待真凭据验证后迁移进 plugins/exchange/）。

端点：GET /v5/user/query-api（查询 API Key 自身信息，只读身份探针，无需交易权限）。

签名算法（V5，HMAC-SHA256，官方文档已确认）：
  prehash = timestamp + api_key + recv_window + queryString
    （本端点无 query 参数，queryString 取空串）
  X-BAPI-SIGN = HMAC_SHA256(secret_key, prehash) 的**小写十六进制**
  请求头：X-BAPI-API-KEY / X-BAPI-TIMESTAMP（毫秒字符串）/ X-BAPI-RECV-WINDOW（"5000"）/ X-BAPI-SIGN

判定依据（官方错误码表已确认）：
  retCode "0"            → valid（result 里取 readOnly/deadlineDays/expiredAt 展示）
  retCode 10003          → invalid（API key 无效）
  retCode 10004          → invalid（签名错误 → secret 不对）
  retCode 10007          → invalid（用户鉴权失败）
  retCode 10005          → error（权限不足：密钥有效但未授予本操作权限；沿用 okx 把权限错误归 error 的处理）
  retCode 10010          → error（API key 未绑定本机 IP，配置问题）
  retCode 10002          → error（时间戳超窗，本机时钟偏差）
  retCode 10006/10001/10000 → error（限流 / 参数 / 服务器）
  HTTP 401               → invalid；HTTP 403 → error（官方注明美国/中国大陆 IP 会被拒）

已查官方文档确认：prehash 拼接顺序与小写十六进制编码、retCode 10002/10003/10004/10005/10006/10007/10010
含义、主网域名 api.bybit.com / api.bytick.com 及区域域名；/v5/user/query-api 返回字段名按文档示例。

参考文档（签名算法）：https://bybit-exchange.github.io/docs/v5/guide
参考文档（端点）：https://bybit-exchange.github.io/docs/v5/user/apikey-info
"""
import hashlib
import hmac
import time
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "Bybit"

# 主网默认域名（官方同时列出 api.bybit.com 与 api.bytick.com，二者皆主网）
DEFAULT_BASE_URL = "https://api.bybit.com"
_PATH = "/v5/user/query-api"

# 明确「凭据/签名错误」的 retCode → invalid
_INVALID_CODES = {"10003", "10004", "10007"}


def _mask(value: str) -> str:
    """仅展示密钥前后缀，避免在列表/日志中回显完整凭据。"""
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


@guarded
def verify(data) -> VerifyResult:
    """Bybit：GET /v5/user/query-api，HMAC-SHA256 小写十六进制签名鉴权。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key:
        return VerifyResult(status="invalid", message="缺少 Secret Key")

    base_url = (data.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    query_string = ""  # 本端点无 query 参数
    prehash = timestamp + api_key + recv_window + query_string
    signature = hmac.new(
        secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }
    with make_client() as client:
        resp = client.get(f"{base_url}{_PATH}", headers=headers)

    payload = safe_json(resp)
    raw_code = payload.get("retCode") if isinstance(payload, dict) else None
    ret_code = str(raw_code) if raw_code is not None else ""

    if ret_code == "0":
        info: dict[str, Any] = {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict):
            for key in ("readOnly", "deadlineDays", "expiredAt"):
                if result.get(key) not in (None, ""):
                    info[key] = result[key]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if ret_code in _INVALID_CODES:
        detail = payload.get("retMsg") if isinstance(payload, dict) else ""
        message = f"凭证无效 (retCode {ret_code})"
        if detail:
            message = f"{message}: {detail}"
        return VerifyResult(status="invalid", message=message)

    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    if resp.status_code == 403:
        return VerifyResult(status="error", message="地域限制，无法从当前网络校验 (HTTP 403)")

    if ret_code:
        detail = payload.get("retMsg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (retCode {ret_code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(resp.status_code, body_snippet(resp))


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
