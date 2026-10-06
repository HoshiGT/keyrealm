"""华为云 AK/SK 验证插件（plugins/cloud/ 类别下的独立子插件）。

只做一件事：**只读**调用对象存储（OBS）的 **列举桶** 接口
``GET https://obs.<区域>.myhuaweicloud.com/``，用回包里的 ``Owner`` 证明这组
AK/SK 当前有效，并顺带给出它属于哪个账号（Domain ID / IAM 用户）。

为什么走 OBS 而不是 IAM：
- IAM 的身份类接口（如 KeystoneListAuthProjects）要 ``X-Auth-Token``，而用 AK/SK
  换 token 的 ``/v3.0/OS-CREDENTIAL/securitytokens`` 会**签发一份临时凭证**（有副作用）；
- OBS 兼容 AWS Signature Version 4，同一个签名算法就能直连，且列举桶是纯读操作，
  不产生任何资源变更，回包还自带 Domain ID 与 IAM 用户名。
- 签名在鉴权阶段校验、权限在之后检查：所以 403 ``AccessDenied`` 说明**AK/SK 是对的**、
  只是这个子用户没有列举桶的权限，不能当无效。

自带完整实现（AWS Signature Version 4 只在这里维护，不依赖类别共享模块）。
仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档：https://support.huaweicloud.com/obs_faq/obs_faq_0274.html
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import xml.etree.ElementTree as ET

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
)

NAME = "华为云"
DEFAULT_REGION = "ap-southeast-3"
SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()

REQUIRED = (("access_key_id", "Access Key ID (AK)"), ("secret_access_key", "Secret Access Key (SK)"))
DISPLAY_FIELD = "access_key_id"

# OBS 明确的「AK/SK 不成立」错误码
INVALID_CODES = {"SignatureDoesNotMatch", "InvalidAccessKeyId", "AccessDeniedException"}

_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def _endpoint(region: str) -> str:
    return f"https://obs.{region}.myhuaweicloud.com"


# ── AWS Signature Version 4（OBS 兼容）─────────────────────

def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str) -> bytes:
    key = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _sign(key, region)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def _headers(access_key: str, secret_key: str, region: str) -> dict:
    """生成列举桶请求的签名头（头名按字母序，与 SignedHeaders 一致）。"""
    host = f"obs.{region}.myhuaweicloud.com"
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")

    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{EMPTY_PAYLOAD_HASH}\n"
        f"x-amz-date:{amzdate}\n"
    )
    canonical_request = "\n".join(["GET", "/", "", canonical_headers, signed_headers, EMPTY_PAYLOAD_HASH])

    scope = f"{datestamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "x-amz-date": amzdate,
        "x-amz-content-sha256": EMPTY_PAYLOAD_HASH,
        "Authorization": (
            f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


# ── 响应解析 ─────────────────────────────────────────────

def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _parse(text: str) -> dict:
    """解析 ListAllMyBucketsResult；解析失败返回空 dict（由调用方走兜底分支）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    owner = {}
    buckets: list[str] = []
    for node in root.iter():
        name = _tag(node)
        if name == "ID" and node.text and node.text.strip() and "domain_id" not in owner:
            owner["domain_id"] = node.text.strip()
        elif name == "DisplayName" and node.text and node.text.strip():
            owner["iam_user"] = node.text.strip()
        elif name == "Name" and node.text and node.text.strip():
            buckets.append(node.text.strip())
    if owner or buckets:
        owner["buckets"] = buckets
    return owner


def _verify(access_key: str, secret_key: str, region: str) -> VerifyResult:
    endpoint = _endpoint(region)
    with make_client() as client:
        resp = client.get(endpoint + "/", headers=_headers(access_key, secret_key, region))

    if resp.status_code == 200:
        info = _parse(resp.text)
        if not info:
            return VerifyResult(status="valid", message="AK/SK 有效", account_info={"region": region})
        buckets = info.pop("buckets", [])
        parts = [f"AK/SK 有效（{region}）"]
        if info.get("iam_user"):
            parts.append(f"IAM 用户 {info['iam_user']}")
        if info.get("domain_id"):
            parts.append(f"Domain ID {info['domain_id']}")
        parts.append(f"{len(buckets)} 个桶")
        account_info = dict(info)
        account_info["region"] = region
        account_info["bucket_count"] = len(buckets)
        if buckets:
            account_info["buckets"] = buckets
        return VerifyResult(status="valid", message="，".join(parts), account_info=account_info)

    code = ""
    try:
        for node in ET.fromstring(resp.text).iter():
            if _tag(node) == "Code" and node.text:
                code = node.text.strip()
                break
    except ET.ParseError:
        pass

    if code in INVALID_CODES:
        return VerifyResult(status="invalid", message=f"AK/SK 无效（{code}）")
    if code == "AccessDenied":
        # 签名已通过、只是该子用户无列举桶权限 ⇒ AK/SK 本身有效
        return VerifyResult(status="valid", message="AK/SK 有效，但无列举桶权限（AccessDenied）")
    detail = code or body_snippet(resp)
    hint = "（若 AK/SK 无误，请确认「OBS 区域」是否是该账号开通的区域）"
    return VerifyResult(
        status="error",
        message=f"无法确认 AK/SK 状态 (HTTP {resp.status_code})：{detail} {hint}" if detail
        else f"无法确认 AK/SK 状态 (HTTP {resp.status_code}) {hint}",
    )


def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    region = str(data.get("region") or "").strip()
    if region and not _REGION_RE.match(region):
        errors.append("OBS 区域格式不正确（形如 ap-southeast-3）")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    access_key = str(data["access_key_id"]).strip()
    secret_key = str(data["secret_access_key"]).strip()
    region = str(data.get("region") or "").strip() or DEFAULT_REGION
    log(f"校验华为云凭据 {mask_secret(access_key)}（OBS {region}）")
    return _verify(access_key, secret_key, region)


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
