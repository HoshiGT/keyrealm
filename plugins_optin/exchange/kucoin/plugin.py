"""KuCoin API 凭证验证插件（plugins_optin/exchange/kucoin/，待真凭据验证后迁移进 plugins/exchange/）。

端点：GET /api/v1/accounts（现货账户列表，只读身份探针）。

签名算法（API Key v2，HMAC-SHA256 + base64，官方文档已确认）：
  timestamp = 毫秒字符串
  str_to_sign = timestamp + "GET" + "/api/v1/accounts"
    （GET 无 body 不拼 body；本端点无 query 参数）
  KC-API-SIGN = base64( HMAC_SHA256(secret_key, str_to_sign) )
  KC-API-PASSPHRASE = base64( HMAC_SHA256(secret_key, passphrase) )   ← v2 需对 passphrase 预哈希
  请求头：KC-API-KEY / KC-API-SIGN / KC-API-TIMESTAMP / KC-API-PASSPHRASE / KC-API-KEY-VERSION: "2"

判定依据（官方错误码表已确认）：
  code "200000"           → valid（data 为账户列表，仅取条数展示）
  code 400006             → invalid（Invalid KC-API-KEY：API Key 无效/不存在）
  code 400005             → invalid（Invalid KC-API-SIGN：签名错误 → Secret 不对）
  code 400007             → invalid（Invalid KC-API-PASSPHRASE：Passphrase 不对）
  code 400008             → error（Invalid KC-API-KEY-VERSION：密钥版本不匹配，可能为旧版 v1 密钥，本插件仅支持 v2）
  code 400002/400003/400004/400009 → error（服务端异常 / 请求太频繁 / 时间戳超窗等，非凭据问题）
  code 401002             → error（IP 未授权/不在白名单，配置问题）
  code 403000/403001      → error（禁止访问 / 地域限制，配置或网络问题）
  code 429000/其它        → error（限流 / 未知）
  HTTP 401               → invalid；HTTP 403 / 429 → error

已查官方文档确认：str_to_sign 拼接（timestamp + method + endpoint + body，GET 无 body）、
v2 的 KC-API-PASSPHRASE 为 base64(HMAC_SHA256(secret, passphrase))、KC-API-KEY-VERSION: "2"、
主网域名 api.kucoin.com；错误码表（200000/400002/400003/400004/400005/400006/400007/400008/
400009/401002/403000/403001/429000）取自官方 Rest API 错误码文档。
仅支持 v2 API Key（v1 已停发，官方默认 v2）。
"""
import base64
import hashlib
import hmac
import time
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "KuCoin"

DEFAULT_BASE_URL = "https://api.kucoin.com"
_PATH = "/api/v1/accounts"

# 明确「凭据/签名/passphrase 错误」的 code → invalid
_INVALID_CODES = {"400005", "400006", "400007"}


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
    """KuCoin：GET /api/v1/accounts，API Key v2 HMAC-SHA256 base64 签名鉴权。"""
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
    str_to_sign = timestamp + "GET" + _PATH
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), str_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    # v2：KC-API-PASSPHRASE 为 base64(HMAC_SHA256(secret, passphrase))
    passphrase_enc = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), passphrase.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    headers = {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": passphrase_enc,
        "KC-API-KEY-VERSION": "2",
    }
    with make_client() as client:
        resp = client.get(f"{base_url}{_PATH}", headers=headers)

    payload = safe_json(resp)
    raw_code = payload.get("code") if isinstance(payload, dict) else None
    code = str(raw_code) if raw_code is not None else ""

    if code == "200000":
        info: dict[str, Any] = {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            info["account_count"] = len(rows)
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

    if code == "400008":
        return VerifyResult(
            status="error",
            message="无法确认凭证状态 (code 400008): 密钥版本不匹配，可能为旧版 v1 密钥（本插件仅支持 v2）",
        )
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
