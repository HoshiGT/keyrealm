"""Bitget API 凭证验证插件（plugins_optin/exchange/bitget/，待真凭据验证后迁移进 plugins/exchange/）。

端点：GET /api/v2/spot/account/assets（现货账户资产，只读身份探针）。

签名算法（V2，HMAC-SHA256 + base64，官方文档与官方 Python SDK 源码均已确认）：
  prehash = timestamp + "GET" + requestPath + body
    （requestPath 含 /api/v2 版本前缀；本端点无 query 参数，GET 的 body 为空串，故只拼前三项）
  ACCESS-SIGN = base64( HMAC_SHA256(secret_key, prehash) )
  请求头：ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP（毫秒字符串）/ ACCESS-PASSPHRASE（明文口令）/ Content-Type: application/json

判定依据（官方错误码表已确认）：
  code "00000"            → valid（data 为资产列表，仅取条数展示）
  code 40006 / 40012 / 40037 / 40041 → invalid（ACCESS_KEY 无效 / 密钥口令错误 / ApiKey 不存在）
  code 40009 / 40003      → invalid（签名错误 / 签名为空）
  code 40014 / 40040 / 40301 / 49005 / 49022 → error（权限不足：密钥有效但未授予本接口权限；沿用 okx 把权限错误归 error）
  code 40018 / 40038 / 49003 → error（IP 未授权 / 未在白名单，配置问题）
  code 40004 / 40005 / 40008 → error（时间戳超窗，本机时钟偏差）
  code 40001 / 40002 / 其它 → error（参数 / 限流 / 未知）
  HTTP 401               → invalid；HTTP 403 / 429 → error

已查官方文档 + 官方 Python SDK（bitgetlimited/v3-bitget-api-sdk 的 client.py / utils.py / consts.py）确认：
prehash 拼接顺序（str(timestamp) + str.upper(method) + request_path + body）、base64 编码、
requestPath 含 /api/v2 前缀、请求头常量名、主网域名 api.bitget.com；错误码表取自官方 Rest API 错误码文档。
"""
import base64
import hashlib
import hmac
import time
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "Bitget"

DEFAULT_BASE_URL = "https://api.bitget.com"
_PATH = "/api/v2/spot/account/assets"

# 明确「凭据/签名错误」的 code → invalid
_INVALID_CODES = {"40003", "40006", "40009", "40012", "40037", "40041"}


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
    if not (data.get("passphrase") or "").strip():
        errors.append("Passphrase 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """Bitget：GET /api/v2/spot/account/assets，HMAC-SHA256 base64 签名鉴权。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    passphrase = (data.get("passphrase") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key or not passphrase:
        return VerifyResult(status="invalid", message="缺少 Secret Key 或 Passphrase")

    base_url = (data.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    timestamp = str(int(time.time() * 1000))
    # 官方 SDK：pre_hash = str(timestamp) + str.upper(method) + request_path + body
    prehash = timestamp + "GET" + _PATH
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    with make_client() as client:
        resp = client.get(f"{base_url}{_PATH}", headers=headers)

    payload = safe_json(resp)
    raw_code = payload.get("code") if isinstance(payload, dict) else None
    code = str(raw_code) if raw_code is not None else ""

    if code == "00000":
        info: dict[str, Any] = {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            info["asset_count"] = len(rows)
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if code in _INVALID_CODES:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        message = f"凭证无效 (code {code})"
        if detail:
            message = f"{message}: {detail}"
        return VerifyResult(status="invalid", message=message)

    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    if resp.status_code in (403, 429):
        return VerifyResult(status="error", message=f"无法从当前网络校验 (HTTP {resp.status_code})")

    if code:
        detail = payload.get("msg") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 (code {code})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(resp.status_code, body_snippet(resp))


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
