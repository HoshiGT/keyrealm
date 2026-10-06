"""Gate.io API 凭证验证插件（plugins_optin/exchange/gate/，待真凭据验证后迁移进 plugins/exchange/）。

端点：GET /api/v4/spot/accounts（现货账户列表，只读身份探针）。

签名算法（V4，HMAC-SHA512 十六进制，官方文档已确认）：
  body_hash = SHA512(body).hexdigest()      （GET 无 body，即 SHA512("") 的十六进制）
  sign_str = "GET" + "\\n" + url + "\\n" + query_string + "\\n" + body_hash + "\\n" + timestamp
    （url 为**含 /api/v4 前缀**的路径；本端点无 query，query_string 取空串；timestamp 为**秒**字符串）
  SIGN = HMAC_SHA512(secret_key, sign_str) 的十六进制
  请求头：KEY / Timestamp（秒字符串）/ SIGN

判定依据（官方错误码 label 已确认）：
  HTTP 200（响应为账户列表）  → valid（仅取账户条数展示）
  label INVALID_KEY / INVALID_SIGNATURE / SIGNATURE_MISMATCH → invalid（密钥无效 / 签名错误 → secret 不对）
  label REQUEST_EXPIRED      → error（时间戳超窗，本机时钟偏差）
  label FORBIDDEN            → error（账户无权限：密钥有效但未授予本接口权限；沿用 okx 把权限错误归 error）
  label INVALID_PARAM_VALUE / TOO_BUSY / 其它 → error（参数 / 服务器繁忙 / 未知）
  HTTP 401                   → invalid；HTTP 403 / 429 → error

已查官方文档确认：sign_str 五段拼接（METHOD / url / query_string / body_sha512_hex / timestamp）、
url 含 /api/v4 前缀、timestamp 为秒、HMAC-SHA512 十六进制；错误 label 表（INVALID_KEY / INVALID_SIGNATURE /
REQUEST_EXPIRED / FORBIDDEN / INVALID_PARAM_VALUE / SIGNATURE_MISMATCH / TOO_BUSY）取自官方错误码文档。
"""
import hashlib
import hmac
import time
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "Gate.io"

DEFAULT_BASE_URL = "https://api.gateio.ws"
_PATH = "/api/v4/spot/accounts"

# 明确「凭据/签名错误」的 label → invalid
_INVALID_LABELS = {"INVALID_KEY", "INVALID_SIGNATURE", "SIGNATURE_MISMATCH"}


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
    """Gate.io：GET /api/v4/spot/accounts，HMAC-SHA512 十六进制签名鉴权。"""
    data = data or {}
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
    if not secret_key:
        return VerifyResult(status="invalid", message="缺少 Secret Key")

    base_url = (data.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    timestamp = str(int(time.time()))  # 秒
    body_hash = hashlib.sha512(b"").hexdigest()  # GET 无 body
    query_string = ""  # 本端点无 query
    sign_str = "\n".join(["GET", _PATH, query_string, body_hash, timestamp])
    signature = hmac.new(
        secret_key.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha512
    ).hexdigest()

    headers = {
        "KEY": api_key,
        "Timestamp": timestamp,
        "SIGN": signature,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    with make_client() as client:
        resp = client.get(f"{base_url}{_PATH}", headers=headers)

    payload = safe_json(resp)
    label = str(payload.get("label", "")) if isinstance(payload, dict) else ""

    if label in _INVALID_LABELS:
        detail = payload.get("message") if isinstance(payload, dict) else ""
        message = f"凭证无效 ({label})"
        if detail:
            message = f"{message}: {detail}"
        return VerifyResult(status="invalid", message=message)

    if resp.status_code == 200:
        # 成功返回裸数组（账户列表），safe_json 对列表返回 None，这里单独取条数
        info: dict[str, Any] = {}
        try:
            rows = resp.json()
        except Exception:  # noqa: BLE001 - 取不到条数就只报有效
            rows = None
        if isinstance(rows, list):
            info["account_count"] = len(rows)
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if resp.status_code == 401:
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {resp.status_code})")
    if resp.status_code in (403, 429):
        return VerifyResult(status="error", message=f"无法从当前网络校验 (HTTP {resp.status_code})")

    if label:
        detail = payload.get("message") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="error",
            message=f"无法确认凭证状态 ({label})" + (f": {detail}" if detail else ""),
        )
    return _unconfirmed(resp.status_code, body_snippet(resp))


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
