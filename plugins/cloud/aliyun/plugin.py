"""阿里云 AccessKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：RPC 签名 v1（HMAC-SHA1），调用只读的 STS GetCallerIdentity，
不产生任何资源变更。仅从 cloud_common 复用通用工具。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from cloud_common import VerifyResult, body_snippet, guarded, make_client, safe_json

# 阿里云 STS RPC 接口地址（公共参数走查询串，GET 请求）
STS_ENDPOINT = "https://sts.aliyuncs.com"

# 认证失败错误码（其余一律按「无法确认」处理）
_INVALID_CODES = {
    "InvalidAccessKeyId.NotFound",
    "InvalidAccessKeyId.Inactive",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
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


def _percent_encode(value: Any) -> str:
    """阿里云 RPC 签名要求的百分号编码（RFC3986，空格 -> %20，'~' 不编码）。"""
    return quote(str(value), safe="~")


def _aliyun_signature(params: dict, access_key_secret: str, method: str = "GET") -> str:
    """按阿里云 RPC 签名 v1 规则生成 Signature。

    canonicalizedQuery 由除 Signature 外的全部参数按 key 升序拼成，
    StringToSign = "GET&%2F&" + percentEncode(canonicalizedQuery)，
    Signature = base64(HMAC-SHA1(StringToSign, access_key_secret + "&"))。
    """
    canonicalized = "&".join(
        f"{_percent_encode(key)}={_percent_encode(value)}"
        for key, value in sorted(params.items())
    )
    string_to_sign = f"{method}&%2F&{_percent_encode(canonicalized)}"
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate(data):
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    if not _field(data, "access_key_id"):
        errors.append("AccessKey ID 不能为空")
    if not _field(data, "access_key_secret"):
        errors.append("AccessKey Secret 不能为空")
    return errors


@guarded
def verify(data):
    access_key_id = _field(data, "access_key_id")
    access_key_secret = _field(data, "access_key_secret")
    if not access_key_id or not access_key_secret:
        return VerifyResult(status="invalid", message="AccessKey ID 或 AccessKey Secret 为空")

    # 公共参数（Signature 最后附加）
    params: dict[str, Any] = {
        "Format": "JSON",
        "Version": "2015-04-01",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": "GetCallerIdentity",
    }
    params["Signature"] = _aliyun_signature(params, access_key_secret)

    with make_client() as client:
        resp = client.get(STS_ENDPOINT, params=params)

    status = resp.status_code
    payload = safe_json(resp) or {}

    # 阿里云错误响应（HTTP 400/401/403）同样以 JSON 返回 Code/Message
    code = str(payload.get("Code") or "").strip()

    if status == 200 and not code:
        info: dict[str, Any] = {}
        for src, dst in (("AccountId", "account_id"), ("Arn", "arn"), ("UserId", "user_id")):
            value = payload.get(src)
            if value:
                info[dst] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if code in _INVALID_CODES or code.startswith("InvalidAccessKeyId") or code == "SignatureDoesNotMatch":
        detail = payload.get("Message") or code
        return VerifyResult(status="invalid", message=f"凭证无效: {detail}")

    if code:
        return VerifyResult(status="error", message=f"无法确认凭证状态: {code}")

    if status == 403:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 403)")
    return _unconfirmed(status, body_snippet(resp))


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    return "阿里云 | AccessKey (AK/SK)"
