"""AWS AccessKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

只做一件事：**只读**调用 STS ``GetCallerIdentity``，用返回的 ARN 证明这组
Access Key 当前有效、并给出它代表谁（root / IAM 用户 / 角色）。

为什么用 STS 而不是列 S3 桶：
- ``sts:GetCallerIdentity`` **不需要任何 IAM 权限**，任何有效密钥都能调用，
  所以拿到 ARN 就等价于「密钥有效」；列桶则要求额外的权限，会把「密钥有效但无
  权限」误判成无效。
- 全局端点 ``sts.amazonaws.com`` 与账号所在地域无关，签名固定用 ``us-east-1``，
  因此不需要用户填地域。

自带完整实现（AWS Signature Version 4 只在这里维护，不依赖类别共享模块）。
全部调用均为只读，不产生任何资源变更。仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档：https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_aws-signing.html
参考文档：https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import xml.etree.ElementTree as ET

import httpx

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
)

NAME = "AWS"
# 全局 STS 端点：签名区域固定 us-east-1，与账号实际所处地域无关
HOST = "sts.amazonaws.com"
REGION = "us-east-1"
SERVICE = "sts"
ALGORITHM = "AWS4-HMAC-SHA256"
BODY = "Action=GetCallerIdentity&Version=2011-06-15"

REQUIRED = (("access_key_id", "Access Key ID"), ("secret_access_key", "Secret Access Key"))
DISPLAY_FIELD = "access_key_id"

# 拿到了明确「密钥不成立」的判定才判无效：
#   InvalidClientTokenId / UnrecognizedClientException → AK 不存在或已被吊销
#   ExpiredToken                                       → 临时凭据已过期
#   SignatureDoesNotMatch / InvalidAccessKeyId         → SK 不对
INVALID_CODES = {
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "ExpiredToken",
    "ExpiredTokenException",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
    "AuthFailure",
}


# ── AWS Signature Version 4 ─────────────────────────────

def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str) -> bytes:
    """HMAC 逐级派生：AWS4+SK → 日期 → 地域 → 服务 → aws4_request。"""
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, REGION)
    key = _hmac(key, SERVICE)
    return _hmac(key, "aws4_request")


def _canonical_request(payload_hash: str, amzdate: str, token: str):
    """构造规范请求与待签串，返回 ``(canonical_request, signed_headers)``。

    头必须**按名排序**且与 signed_headers 一致：content-type < host < x-amz-date
    < x-amz-security-token。
    """
    headers = [
        ("content-type", "application/x-www-form-urlencoded; charset=utf-8"),
        ("host", HOST),
        ("x-amz-date", amzdate),
    ]
    if token:
        headers.append(("x-amz-security-token", token))
    headers.sort(key=lambda item: item[0])

    canonical_headers = "".join(f"{name}:{value}\n" for name, value in headers)
    signed_headers = ";".join(name for name, _ in headers)
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
    )
    return canonical_request, signed_headers


def _authorization(access_key: str, secret_key: str, token: str, now: datetime.datetime) -> dict:
    """生成本次请求的完整头（含 Authorization），便于离线单测。"""
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    canonical_request, signed_headers = _canonical_request(payload_hash, amzdate, token)

    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "X-Amz-Date": amzdate,
        "Authorization": (
            f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if token:
        headers["X-Amz-Security-Token"] = token
    return headers


# ── 响应解析 ─────────────────────────────────────────────

def _xml_fields(text: str) -> dict:
    """把 STS 的 XML 回包拍平成一层的 ``{标签: 文本}``（自动剥掉命名空间）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    flat: dict[str, str] = {}
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if node.text and node.text.strip():
            flat.setdefault(tag, node.text.strip())
    return flat


def _verify(access_key: str, secret_key: str, token: str) -> VerifyResult:
    headers = _authorization(access_key, secret_key, token, datetime.datetime.now(datetime.timezone.utc))
    with make_client() as client:
        resp = client.post(f"https://{HOST}/", headers=headers, content=BODY)

    fields = _xml_fields(resp.text)
    arn = fields.get("Arn")
    if resp.status_code == 200 and arn:
        info = {"Arn": arn}
        if fields.get("Account"):
            info["Account"] = fields["Account"]
        if fields.get("UserId"):
            info["UserId"] = fields["UserId"]
        return VerifyResult(status="valid", message=f"密钥有效：{arn}", account_info=info)

    code = fields.get("Code") or ""
    detail = fields.get("Message") or body_snippet(resp)
    if code in INVALID_CODES:
        return VerifyResult(
            status="invalid",
            message=f"密钥无效（{code}）：{detail}" if detail else f"密钥无效（{code}）",
        )
    # 签名已被接受、只是被 IAM 策略拒绝：密钥本身是真的
    if code in ("AccessDenied", "AccessDeniedException"):
        return VerifyResult(
            status="valid",
            message=f"密钥有效，但被 IAM 策略拒绝调用 STS：{detail}" if detail else "密钥有效，但被 IAM 策略拒绝",
        )
    if resp.status_code == 403 and not code:
        return VerifyResult(status="invalid", message="密钥无效（HTTP 403）")
    return VerifyResult(
        status="error",
        message=f"无法确认密钥状态 (HTTP {resp.status_code})：{detail}" if detail
        else f"无法确认密钥状态 (HTTP {resp.status_code})",
    )


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    access_key = str(data.get("access_key_id") or "").strip().upper()
    if access_key.startswith("ASIA") and not str(data.get("session_token") or "").strip():
        errors.append("临时凭据（ASIA… 开头）必须填写 Session Token")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    access_key = str(data["access_key_id"]).strip()
    secret_key = str(data["secret_access_key"]).strip()
    token = str(data.get("session_token") or "").strip()

    if access_key.upper().startswith("ASIA") and not token:
        return VerifyResult(
            status="error",
            message="以 ASIA 开头的 Access Key ID 属临时凭据，缺少 Session Token 无法签名验证",
        )
    log(f"校验 AWS 凭据 {mask_secret(access_key)}")
    return _verify(access_key, secret_key, token)


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
