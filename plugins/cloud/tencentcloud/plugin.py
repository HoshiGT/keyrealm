"""腾讯云 SecretId/SecretKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：TC3-HMAC-SHA256 签名，调用只读的 STS GetCallerIdentity，
不产生任何资源变更。仅从 cloud_common 复用通用工具。
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from cloud_common import VerifyResult, body_snippet, guarded, make_client, safe_json

# 腾讯云 STS 接口地址（TC3 签名走请求头）
STS_HOST = "sts.tencentcloudapi.com"
STS_SERVICE = "sts"
STS_ACTION = "GetCallerIdentity"
STS_VERSION = "2018-08-13"
STS_REGION = "ap-guangzhou"

# 不属于「凭证无效」的错误码（时钟偏移、限流等不应判定为无效）
_NON_CREDENTIAL_CODES = {
    "AuthFailure.SignatureExpire",
    "RequestLimitExceeded",
    "InternalError",
}


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _unconfirmed(status: int, snippet: str) -> VerifyResult:
    message = f"无法确认凭证状态 (HTTP {status})"
    if snippet:
        message = f"{message}: {snippet}"
    return VerifyResult(status="error", message=message)


def _tencent_authorization(
    secret_id: str,
    secret_key: str,
    timestamp: int,
    date: str,
    payload: str,
    content_type: str,
) -> str:
    """生成腾讯云 TC3-HMAC-SHA256 Authorization 头。"""
    canonical_headers = f"content-type:{content_type}\nhost:{STS_HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{STS_SERVICE}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, STS_SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not _field(data, "secret_id"):
        errors.append("SecretId 不能为空")
    if not _field(data, "secret_key"):
        errors.append("SecretKey 不能为空")
    return errors


@guarded
def verify(data):
    secret_id = _field(data, "secret_id")
    secret_key = _field(data, "secret_key")
    if not secret_id or not secret_key:
        return VerifyResult(status="invalid", message="SecretId 或 SecretKey 为空")

    payload = "{}"
    content_type = "application/json; charset=utf-8"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

    authorization = _tencent_authorization(
        secret_id, secret_key, timestamp, date, payload, content_type
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": STS_HOST,
        "X-TC-Action": STS_ACTION,
        "X-TC-Version": STS_VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": STS_REGION,
    }

    with make_client() as client:
        resp = client.post(
            f"https://{STS_HOST}/", headers=headers, content=payload
        )

    status = resp.status_code
    payload_json = safe_json(resp) or {}
    response_body = payload_json.get("Response")
    if not isinstance(response_body, dict):
        response_body = {}

    # 腾讯云多数业务错误以 HTTP 200 + Response.Error 返回
    error = response_body.get("Error")
    if isinstance(error, dict):
        code = str(error.get("Code") or "").strip()
        message = error.get("Message") or code or "未知错误"
        if code in _NON_CREDENTIAL_CODES:
            return VerifyResult(status="error", message=f"无法确认凭证状态: {message}")
        if code.startswith("AuthFailure") or "InvalidSecretId" in code:
            return VerifyResult(status="invalid", message=f"凭证无效: {message}")
        return VerifyResult(status="error", message=f"无法确认凭证状态: {code or message}")

    if status == 200:
        info: dict[str, Any] = {}
        for src, dst in (
            ("Arn", "arn"),
            ("AccountId", "account_id"),
            ("PrincipalId", "principal_id"),
        ):
            value = response_body.get(src)
            if value:
                info[dst] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if status in (401, 403):
        return VerifyResult(status="invalid", message=f"凭证无效 (HTTP {status})")
    return _unconfirmed(status, body_snippet(resp))


# ── 可选操作：查看调用者身份（只读）─────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的调用者身份查询）。"""
    data = data or {}
    if not (_field(data, "secret_id") and _field(data, "secret_key")):
        return []
    return [{
        "id": "whoami",
        "name": "查看调用者身份",
        "kind": "form",
        "submit": "查询",
        "fields": [],
    }]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作：调用 STS GetCallerIdentity 查看当前凭据身份（只读）。"""
    data = data or {}
    try:
        if action_id != "whoami":
            return {"ok": False, "error": "未知操作"}

        secret_id = _field(data, "secret_id")
        secret_key = _field(data, "secret_key")
        if not secret_id or not secret_key:
            return {"ok": False, "error": "SecretId 或 SecretKey 为空"}

        # 复用与 verify 相同的 TC3-HMAC-SHA256 签名逻辑，仅请求体构造重复一次
        payload = "{}"
        content_type = "application/json; charset=utf-8"
        timestamp = int(datetime.now(timezone.utc).timestamp())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        headers = {
            "Authorization": _tencent_authorization(
                secret_id, secret_key, timestamp, date, payload, content_type
            ),
            "Content-Type": content_type,
            "Host": STS_HOST,
            "X-TC-Action": STS_ACTION,
            "X-TC-Version": STS_VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": STS_REGION,
        }

        with make_client() as client:
            resp = client.post(f"https://{STS_HOST}/", headers=headers, content=payload)

        body = safe_json(resp) or {}
        response_body = body.get("Response")
        response_body = response_body if isinstance(response_body, dict) else {}

        # 腾讯云多数业务错误以 HTTP 200 + Response.Error 返回
        error = response_body.get("Error")
        if isinstance(error, dict):
            detail = str(error.get("Message") or error.get("Code") or f"HTTP {resp.status_code}")
            return {"ok": False, "error": f"查询失败：{_redact(detail, secret_id, secret_key)}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"查询失败：HTTP {resp.status_code}"}

        lines = ["调用者身份："]
        for key in ("Arn", "AccountId", "PrincipalId", "UserId"):
            if response_body.get(key):
                lines.append(f"{key}: {response_body[key]}")
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    return "腾讯云 | SecretId/SecretKey"
