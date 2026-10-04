"""云服务商类别共享模块：各云厂商凭据验证处理器。

plugins/cloud/ 下的每个子插件（aliyun、tencentcloud、cloudflare……）都是独立插件，
通过 `from cloud_common import ...` 复用本模块；删除任一子插件目录
不影响其他子插件运行。

支持阿里云（AccessKey，RPC 签名 v1 / HMAC-SHA1）、腾讯云（SecretId/SecretKey，
TC3-HMAC-SHA256）、Cloudflare（API Token 或 Global API Key）。
验证均调用只读的身份查询接口（STS GetCallerIdentity / user），
不产生任何资源变更，也不会联网请求真实第三方 API（由调用方按需触发）。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

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

# 阿里云 STS RPC 接口地址（公共参数走查询串，GET 请求）
ALIYUN_STS_ENDPOINT = "https://sts.aliyuncs.com"

# 腾讯云 STS 接口地址（TC3 签名走请求头）
TENCENT_STS_HOST = "sts.tencentcloudapi.com"
TENCENT_STS_SERVICE = "sts"
TENCENT_STS_ACTION = "GetCallerIdentity"
TENCENT_STS_VERSION = "2018-08-13"
TENCENT_STS_REGION = "ap-guangzhou"

# Cloudflare API 基地址
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

# 阿里云：认证失败错误码（其余一律按「无法确认」处理）
_ALIYUN_INVALID_CODES = {
    "InvalidAccessKeyId.NotFound",
    "InvalidAccessKeyId.Inactive",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
}

# 腾讯云：不属于「凭证无效」的错误码（时钟偏移、限流等不应判定为无效）
_TENCENT_NON_CREDENTIAL_CODES = {
    "AuthFailure.SignatureExpire",
    "RequestLimitExceeded",
    "InternalError",
}


SERVICE_CONFIGS = {
    "aliyun": {
        "name": "阿里云",
        "summary_hint": "AccessKey (AK/SK)",
    },
    "tencentcloud": {
        "name": "腾讯云",
        "summary_hint": "SecretId/SecretKey",
    },
    "cloudflare": {
        "name": "Cloudflare",
        "summary_hint": "API 凭据",
    },
}


def _log(message: str) -> None:
    print(f"[cloud] {message}", file=sys.stderr)


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


def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


# ── 阿里云：RPC 签名 v1（HMAC-SHA1） ─────────────────────

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


def _verify_aliyun(data: dict) -> VerifyResult:
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

    with _client() as client:
        resp = client.get(ALIYUN_STS_ENDPOINT, params=params)

    status = resp.status_code
    payload = _safe_json(resp) or {}

    # 阿里云错误响应（HTTP 400/401/403）同样以 JSON 返回 Code/Message
    code = str(payload.get("Code") or "").strip()

    if status == 200 and not code:
        info: dict[str, Any] = {}
        for src, dst in (("AccountId", "account_id"), ("Arn", "arn"), ("UserId", "user_id")):
            value = payload.get(src)
            if value:
                info[dst] = value
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    if code in _ALIYUN_INVALID_CODES or code.startswith("InvalidAccessKeyId") or code == "SignatureDoesNotMatch":
        detail = payload.get("Message") or code
        return VerifyResult(status="invalid", message=f"凭证无效: {detail}")

    if code:
        return VerifyResult(status="error", message=f"无法确认凭证状态: {code}")

    if status == 403:
        return VerifyResult(status="invalid", message="凭证无效 (HTTP 403)")
    return _unconfirmed(status, _body_snippet(resp))


# ── 腾讯云：TC3-HMAC-SHA256 ──────────────────────────────

def _tencent_authorization(
    secret_id: str,
    secret_key: str,
    timestamp: int,
    date: str,
    payload: str,
    content_type: str,
) -> str:
    """生成腾讯云 TC3-HMAC-SHA256 Authorization 头。"""
    canonical_headers = f"content-type:{content_type}\nhost:{TENCENT_STS_HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{TENCENT_STS_SERVICE}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, TENCENT_STS_SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _verify_tencentcloud(data: dict) -> VerifyResult:
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
        "Host": TENCENT_STS_HOST,
        "X-TC-Action": TENCENT_STS_ACTION,
        "X-TC-Version": TENCENT_STS_VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": TENCENT_STS_REGION,
    }

    with _client() as client:
        resp = client.post(
            f"https://{TENCENT_STS_HOST}/", headers=headers, content=payload
        )

    status = resp.status_code
    payload_json = _safe_json(resp) or {}
    response_body = payload_json.get("Response")
    if not isinstance(response_body, dict):
        response_body = {}

    # 腾讯云多数业务错误以 HTTP 200 + Response.Error 返回
    error = response_body.get("Error")
    if isinstance(error, dict):
        code = str(error.get("Code") or "").strip()
        message = error.get("Message") or code or "未知错误"
        if code in _TENCENT_NON_CREDENTIAL_CODES:
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
    return _unconfirmed(status, _body_snippet(resp))


# ── Cloudflare：API Token 或 Global API Key ──────────────

def _cloudflare_use_global(data: dict) -> Optional[bool]:
    """判定凭据模式：True=Global API Key，False=API Token，None=未提供可用凭据。

    显式 auth_mode 优先；未选择时按已填字段推断。
    """
    auth_mode = _field(data, "auth_mode").lower()
    api_token = _field(data, "api_token")
    email = _field(data, "email")
    global_key = _field(data, "global_api_key")

    if auth_mode == "global":
        return True
    if auth_mode == "token":
        return False
    if api_token:
        return False
    if email and global_key:
        return True
    return None


def _verify_cloudflare(data: dict) -> VerifyResult:
    use_global = _cloudflare_use_global(data)
    if use_global is None:
        return VerifyResult(
            status="invalid",
            message="未提供可用的 Cloudflare 凭据（需要 API Token，或 Email + Global API Key）",
        )

    if use_global:
        email = _field(data, "email")
        global_key = _field(data, "global_api_key")
        if not email or not global_key:
            return VerifyResult(
                status="invalid", message="Global API Key 模式需要同时填写 Email 与 Global API Key"
            )
        url = f"{CLOUDFLARE_API_BASE}/user"
        headers = {"X-Auth-Email": email, "X-Auth-Key": global_key}
        mode = "global"
    else:
        api_token = _field(data, "api_token")
        if not api_token:
            return VerifyResult(status="invalid", message="API Token 模式下 API Token 不能为空")
        url = f"{CLOUDFLARE_API_BASE}/user/tokens/verify"
        headers = {"Authorization": f"Bearer {api_token}"}
        mode = "token"

    with _client() as client:
        resp = client.get(url, headers=headers)

    status = resp.status_code
    payload = _safe_json(resp) or {}
    success = payload.get("success")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

    if success is True:
        info: dict[str, Any] = {}
        if mode == "token":
            if result.get("id"):
                info["token_id"] = result["id"]
            if result.get("status"):
                info["token_status"] = result["status"]
        else:
            if result.get("email"):
                info["email"] = result["email"]
            if result.get("id"):
                info["user_id"] = result["id"]
        return VerifyResult(status="valid", message="凭证有效", account_info=info or None)

    # 认证失败：success=false（通常 HTTP 401/403）
    if success is False or status in (401, 403):
        errors = payload.get("errors")
        detail = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = errors[0].get("message") or ""
        message = "凭证无效" if not detail else f"凭证无效: {detail}"
        return VerifyResult(status="invalid", message=message)

    return _unconfirmed(status, _body_snippet(resp))


_SERVICE_VERIFIERS = {
    "aliyun": _verify_aliyun,
    "tencentcloud": _verify_tencentcloud,
    "cloudflare": _verify_cloudflare,
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

    if service == "aliyun":
        if not _field(data, "access_key_id"):
            errors.append("AccessKey ID 不能为空")
        if not _field(data, "access_key_secret"):
            errors.append("AccessKey Secret 不能为空")
    elif service == "tencentcloud":
        if not _field(data, "secret_id"):
            errors.append("SecretId 不能为空")
        if not _field(data, "secret_key"):
            errors.append("SecretKey 不能为空")
    elif service == "cloudflare":
        use_global = _cloudflare_use_global(data)
        if use_global is None:
            errors.append("请选择认证方式，并填写对应的凭据（API Token，或 Email + Global API Key）")
        elif use_global:
            if not _field(data, "email"):
                errors.append("Global API Key 模式下 Email 不能为空")
            if not _field(data, "global_api_key"):
                errors.append("Global API Key 模式下 Global API Key 不能为空")
        elif not _field(data, "api_token"):
            errors.append("API Token 模式下 API Token 不能为空")

    return errors


def verify_service(service: str, data: dict) -> VerifyResult:
    """验证指定服务的云凭据是否有效。保证不抛出异常。"""
    try:
        data = data or {}
        config = SERVICE_CONFIGS.get(service)
        if config is None:
            return VerifyResult(status="error", message=f"不支持的服务: {service}")

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
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    data = data or {}
    config = SERVICE_CONFIGS.get(service, {})
    name = config.get("name", service)

    if service == "cloudflare":
        # 展示用推断：显式 global 或「只填了 Global API Key」都按 global 展示
        auth_mode = _field(data, "auth_mode").lower()
        is_global = auth_mode == "global" or (
            auth_mode != "token"
            and bool(_field(data, "global_api_key"))
            and not _field(data, "api_token")
        )
        mode = "Global API Key" if is_global else "API Token"
        return f"{name} | {mode}"

    return f"{name} | {config.get('summary_hint', '凭据')}"
