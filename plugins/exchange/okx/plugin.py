"""OKX API 凭证验证插件（plugins/exchange/ 类别下的独立子插件）。

OKX：GET /api/v5/account/balance，HMAC-SHA256（base64）签名鉴权，
需 Passphrase。签名算法与判定逻辑随插件自带，不依赖 exchange_common。
"""
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from exchange_common import VerifyResult, body_snippet, guarded, make_client, safe_json

SERVICE_NAME = "OKX"


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
    if not (data.get("passphrase") or "").strip():
        errors.append("Passphrase 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """OKX：GET /api/v5/account/balance，HMAC-SHA256 签名鉴权。"""
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    passphrase = (data.get("passphrase") or "").strip()
    if not api_key:
        return VerifyResult(status="invalid", message="API Key 为空")
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
    with make_client() as client:
        resp = client.get(url, headers=headers)

    payload = safe_json(resp)
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
    return _unconfirmed(resp.status_code, body_snippet(resp))


def get_display_summary(data) -> str:
    """凭据列表中的简短展示文本（不回显完整密钥）。"""
    data = data or {}
    api_key = _mask(data.get("api_key") or "")
    if not api_key:
        return SERVICE_NAME
    return f"{SERVICE_NAME} | {api_key}"
