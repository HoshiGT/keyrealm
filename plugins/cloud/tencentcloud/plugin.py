"""腾讯云 SecretId/SecretKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：**两套签名**都在本文件内维护，不依赖 cloud_common：

- TC3-HMAC-SHA256（``Authorization: TC3-HMAC-SHA256 Credential=...``）——STS / CVM 等云 API 走这套；
- COS 签名（``q-sign-algorithm=sha1``，HMAC-SHA1）——对象存储走这套，规则完全不同，故单独实现。

全部调用均为只读，不产生任何资源变更。仅从 cloud_common 复用通用工具。

本插件提供的操作（``actions``）：

- ``whoami``          查看调用者身份（STS GetCallerIdentity + 权限探测）
- ``list_instances``  云主机实例列表（CVM DescribeInstances，表单可改地域）
- ``list_buckets``    对象存储桶列表（COS 桶列表）
- ``list_objects``    对象预览（COS 对象列表，填桶名直查或从桶列表行内「预览」进入）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from cloud_common import (
    VerifyResult,
    body_snippet,
    guarded,
    make_client,
    safe_json,
)

# 腾讯云 STS 接口地址（TC3 签名走请求头）
STS_HOST = "sts.tencentcloudapi.com"
STS_SERVICE = "sts"
STS_ACTION = "GetCallerIdentity"
STS_VERSION = "2018-08-13"
STS_REGION = "ap-guangzhou"

# CVM（云主机）接口，与 STS 共用同一套 TC3 签名
CVM_HOST = "cvm.tencentcloudapi.com"
CVM_SERVICE = "cvm"
CVM_VERSION = "2017-03-12"

# COS（对象存储）全局端点：桶列表走这里
COS_GLOBAL_HOST = "service.cos.myqcloud.com"

# 默认地域（CVM / COS 均先按此探测）
DEFAULT_REGION = "ap-guangzhou"

# 权限探测的单请求超时（秒）——探测要「整体十几秒内」结束，不能用默认的 20 秒 ×3
_PROBE_TIMEOUT = 6.0

# 不属于「凭证无效」的错误码（时钟偏移、限流等不应判定为无效）
_NON_CREDENTIAL_CODES = {
    "AuthFailure.SignatureExpire",
    "RequestLimitExceeded",
    "InternalError",
}

# 权限探测结论：ok / denied（明确无权限）/ bad_cred（凭据被拒）/ unknown（拿不准 → fail-open）
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

# COS 错误码 → 结论
_COS_BAD_CRED_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch"}

# 桶名 / 地域白名单式校验：避免把接口返回的字符串直接拼进 URL
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9\-]+$")


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


# ── TC3-HMAC-SHA256 签名（host / service 可换，供 STS 与 CVM 共用）──

def _tc3_authorization(
    secret_id: str,
    secret_key: str,
    host: str,
    service: str,
    timestamp: int,
    date: str,
    payload: str,
    content_type: str,
) -> str:
    """生成腾讯云 TC3-HMAC-SHA256 Authorization 头（host/service 按目标接口传入）。"""
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _tencent_authorization(
    secret_id: str,
    secret_key: str,
    timestamp: int,
    date: str,
    payload: str,
    content_type: str,
) -> str:
    """生成 STS 的 TC3-HMAC-SHA256 Authorization 头（保持原有调用点签名不变）。"""
    return _tc3_authorization(
        secret_id, secret_key, STS_HOST, STS_SERVICE, timestamp, date, payload, content_type
    )


def _tc3_call(
    client,
    host: str,
    service: str,
    action: str,
    version: str,
    region: str,
    payload_dict: dict | None,
    secret_id: str,
    secret_key: str,
    timeout: float | None = None,
):
    """发起一次 TC3 签名的只读 POST 请求（腾讯云云 API 通用形态）。"""
    payload = json.dumps(payload_dict or {}, separators=(",", ":"), ensure_ascii=False)
    content_type = "application/json; charset=utf-8"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

    headers = {
        "Authorization": _tc3_authorization(
            secret_id, secret_key, host, service, timestamp, date, payload, content_type
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
    }
    if region:
        headers["X-TC-Region"] = region

    kwargs: dict[str, Any] = {"headers": headers, "content": payload}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.post(f"https://{host}/", **kwargs)


# ── COS 签名（q-sign-algorithm=sha1）─────────────────────

def _sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _hmac_sha1_hex(key: str, text: str) -> str:
    return hmac.new(key.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).hexdigest()


def _cos_quote(value: Any) -> str:
    """COS 签名里的 key/value 百分号编码（保留 -_.~，其余含 / 一并编码，与官方 SDK 一致）。"""
    return quote(str(value), safe="-_.~")


def _cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    path: str,
    params: dict,
    headers: dict,
    sign_time: str,
) -> str:
    """COS 签名（与官方 cos-python-sdk-v5 逐字节对齐）。

    signKey = HMAC-SHA1(SecretKey, KeyTime)          ← 单次 HMAC
    httpString = method\\npath\\nparams\\nheaders\\n
    stringToSign = "sha1"\\n + signTime\\n + sha1(httpString)\\n
    signature = HMAC-SHA1(signKey, stringToSign)

    注意：任务描述里的 ``HMAC(HMAC(SecretKey, signTime), keyTime)`` 是**双重** HMAC，
    与官方实现不符；已按官方 SDK 更正为单次 HMAC，并用 SDK 做了逐字节校验。
    """
    key_time = sign_time
    sign_key = _hmac_sha1_hex(secret_key, key_time)

    param_keys = sorted(params.keys())
    http_params = "&".join(f"{_cos_quote(k)}={_cos_quote(params[k])}" for k in param_keys)

    normalized = {str(k).lower(): v for k, v in headers.items()}
    header_keys = sorted(normalized.keys())
    http_headers = "&".join(f"{_cos_quote(k)}={_cos_quote(normalized[k])}" for k in header_keys)

    http_string = f"{method.lower()}\n{path}\n{http_params}\n{http_headers}\n"
    string_to_sign = f"sha1\n{sign_time}\n{_sha1_hex(http_string)}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)

    return (
        "q-sign-algorithm=sha1&q-ak={ak}&q-sign-time={st}&q-key-time={kt}"
        "&q-header-list={hl}&q-url-param-list={pl}&q-signature={sg}"
    ).format(
        ak=secret_id,
        st=sign_time,
        kt=key_time,
        hl=";".join(header_keys),
        pl=";".join(param_keys),
        sg=signature,
    )


def _cos_get(
    client,
    host: str,
    path: str,
    params: dict | None,
    secret_id: str,
    secret_key: str,
    timeout: float | None = None,
):
    """发起一次 COS 签名的只读 GET（签名只覆盖 host 头 + 查询参数）。"""
    now = int(datetime.now(timezone.utc).timestamp())
    sign_time = f"{now};{now + 3600}"
    signed_headers = {"host": host}
    authorization = _cos_authorization(
        secret_id, secret_key, "GET", path, params or {}, signed_headers, sign_time
    )
    headers = {"Host": host, "Authorization": authorization}
    kwargs: dict[str, Any] = {"params": params or None, "headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(f"https://{host}{path}", **kwargs)


# ── XML 解析（COS 返回 XML；用本地名匹配，绕开 namespace 差异）──

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_root(text: str):
    try:
        return ET.fromstring(text)
    except Exception:
        return None


def _xml_findall(text_or_elem, local_name: str) -> list:
    """返回所有本地名匹配的后代元素（namespace 无关）。"""
    root = text_or_elem
    if isinstance(root, str):
        root = _xml_root(root)
    if root is None:
        return []
    return [elem for elem in root.iter() if _local_name(elem.tag) == local_name]


def _xml_text(elem, local_name: str) -> str:
    """取某个后代元素的文本（第一个，忽略大小写差异的常见变体）。"""
    if elem is None:
        return ""
    target = local_name.lower()
    for child in elem.iter():
        if _local_name(child.tag).lower() == target:
            return (child.text or "").strip()
    return ""


def _xml_error(text: str) -> tuple[str, str]:
    """COS 错误响应 <Error><Code/><Message/></Error> → (code, message)。"""
    root = _xml_root(text)
    if root is None:
        return "", ""
    return _xml_text(root, "Code"), _xml_text(root, "Message")


# ── 权限探测（只读；拿不准 → fail-open）─────────────────

def _tc3_conclusion(resp) -> str:
    """把 TC3 响应归一成探测结论。"""
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    error = response_body.get("Error")
    if resp.status_code == 200 and not isinstance(error, dict):
        return _PROBE_OK
    if isinstance(error, dict):
        code = str(error.get("Code") or "")
        if code.startswith("AuthFailure") or "InvalidSecretId" in code:
            return _PROBE_BAD_CRED
        if code.startswith("UnauthorizedOperation") or code.startswith("FailedOperation.NotAllow"):
            return _PROBE_DENIED
        return _PROBE_UNKNOWN
    if resp.status_code == 401:
        return _PROBE_BAD_CRED
    if resp.status_code == 403:
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _tc3_probe(client, host, service, action, version, region, payload, ak, sk) -> str:
    """探测一次 TC3 接口，返回结论字符串；异常一律 unknown（不抛出）。"""
    try:
        resp = _tc3_call(
            client, host, service, action, version, region, payload, ak, sk, _PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - 探测失败不能让整个 capabilities 崩
        return _PROBE_UNKNOWN
    return _tc3_conclusion(resp)


def _cos_probe_buckets(client, ak, sk) -> str:
    """探测 COS 桶列表（只读），返回结论字符串。"""
    try:
        resp = _cos_get(client, COS_GLOBAL_HOST, "/", {}, ak, sk, _PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    code, _ = _xml_error(resp.text)
    if code in _COS_BAD_CRED_CODES:
        return _PROBE_BAD_CRED
    if code in ("AccessDenied", "Forbidden"):
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _probe_all(ak: str, sk: str) -> dict:
    """依次只读探测 STS / CVM / COS，返回 {action_id: 结论}（彼此独立）。"""
    status: dict[str, str] = {}
    with make_client() as client:
        status["whoami"] = _tc3_probe(
            client, STS_HOST, STS_SERVICE, STS_ACTION, STS_VERSION, STS_REGION, {}, ak, sk
        )
        status["list_instances"] = _tc3_probe(
            client,
            CVM_HOST,
            CVM_SERVICE,
            "DescribeInstances",
            CVM_VERSION,
            DEFAULT_REGION,
            {"Limit": 1},
            ak,
            sk,
        )
        status["list_buckets"] = _cos_probe_buckets(client, ak, sk)
    # 对象预览与桶列表是同一个 COS 权限位
    status["list_objects"] = status["list_buckets"]
    return status


def capabilities(data: dict) -> dict:
    """返回当前凭据**真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    探测 STS / CVM / COS 三个只读接口，各自独立 try：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 凭据被拒」→ 隐藏该操作；
    - 网络异常或返回看不懂 → **unknown**，按可用处理（fail-open）。

    若三个接口全都拿不准，则直接抛异常，交给框架按「未知 → 全部可用」兜底。
    """
    data = data or {}
    ak = _field(data, "secret_id")
    sk = _field(data, "secret_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 SecretId 或 SecretKey"}

    status = _probe_all(ak, sk)
    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定 —— 让框架按「未知」处理，绝不返回空列表把操作全隐藏
        raise RuntimeError("腾讯云权限探测全部失败（网络异常或响应无法识别）")

    if status.get("whoami") == _PROBE_BAD_CRED:
        # 基础身份接口都明确拒了这对密钥 —— 同一对密钥在 CVM/COS 上必然也被拒，
        # 此时剩下几个探针的 unknown 不是「拿不准」而是「密钥已废」，没必要假装可用。
        return {"available": [], "note": "SecretId/SecretKey 无效或已禁用，任何操作都会失败"}

    available = [
        action_id
        for action_id, value in status.items()
        if value not in (_PROBE_DENIED, _PROBE_BAD_CRED)
    ]

    if any(value == _PROBE_BAD_CRED for value in status.values()):
        note = "凭证被拒绝，相关操作已隐藏"
    else:
        rights = []
        if status.get("list_instances") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
            rights.append("云主机管理权限")
        if status.get("list_buckets") not in (_PROBE_DENIED, _PROBE_BAD_CRED):
            rights.append("对象存储权限")
        note = f"具备{'、'.join(rights)}" if rights else "未探测到额外资源权限"
    return {"available": available, "note": note}


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

    with make_client() as client:
        resp = _tc3_call(
            client,
            STS_HOST,
            STS_SERVICE,
            STS_ACTION,
            STS_VERSION,
            STS_REGION,
            {},
            secret_id,
            secret_key,
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


# ── 操作 ────────────────────────────────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _tc3_failure(resp, ak: str, sk: str) -> str:
    """把 TC3 失败响应转成一句脱敏后的错误信息。"""
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    error = response_body.get("Error")
    if isinstance(error, dict):
        detail = str(error.get("Message") or error.get("Code") or f"HTTP {resp.status_code}")
    else:
        detail = f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


def _cos_failure(resp, ak: str, sk: str) -> str:
    """把 COS 失败响应（XML）转成一句脱敏后的错误信息。"""
    code, message = _xml_error(resp.text)
    detail = message or code or f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


_WHOAMI_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    {"key": "InstanceName", "label": "实例名"},
    {"key": "InstanceState", "label": "状态", "width": 100, "type": "tag"},
    {"key": "PublicIp", "label": "公网 IP", "width": 130, "mono": True},
    {"key": "InstanceType", "label": "规格"},
]

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "region", "label": "地域", "width": 120},
]

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

_OBJECT_ROW_ACTION = {
    "id": "list_objects",
    "label": "预览",
    "params": {"bucket": "$row.name", "region": "$row.region"},
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的身份 / 实例 / 对象存储查询）。"""
    data = data or {}
    if not (_field(data, "secret_id") and _field(data, "secret_key")):
        return []
    return [
        {
            "id": "whoami",
            "name": "查看调用者身份",
            "kind": "list",
            "group": "身份",
            "order": 10,
            "description": "STS GetCallerIdentity 身份信息 + 只读权限探测",
            "columns": _WHOAMI_COLUMNS,
            "empty_text": "未获取到身份信息",
        },
        {
            "id": "list_instances",
            "name": "云主机实例列表",
            "kind": "list",
            "group": "计算",
            "order": 20,
            "description": f"CVM DescribeInstances（地域默认 {DEFAULT_REGION}，可在表单里改）",
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「查询」时并入参数
            "fields": [
                {
                    "key": "region",
                    "label": "地域",
                    "placeholder": f"默认 {DEFAULT_REGION}，如 ap-shanghai / ap-beijing",
                }
            ],
            "submit": "查询",
            "empty_text": f"暂无云主机实例（默认地域 {DEFAULT_REGION}）",
        },
        {
            "id": "list_buckets",
            "name": "对象存储桶列表",
            "kind": "list",
            "group": "存储",
            "order": 30,
            "description": "COS 桶列表（只读）",
            "columns": _BUCKET_COLUMNS,
            "row_actions": [_OBJECT_ROW_ACTION],
            "empty_text": "暂无对象存储桶",
        },
        {
            "id": "list_objects",
            "name": "对象预览",
            "kind": "list",
            "group": "存储",
            "order": 40,
            "description": "COS 对象列表（填桶名直接查，或从「对象存储桶列表」行内「预览」进入）",
            "columns": _OBJECT_COLUMNS,
            "fields": [
                {"key": "bucket", "label": "桶名", "placeholder": "如 mybucket-1250000000"},
                {
                    "key": "region",
                    "label": "地域",
                    "placeholder": f"默认 {DEFAULT_REGION}，如 ap-shanghai",
                },
            ],
            "submit": "查询",
            "empty_text": "该桶暂无对象（仅显示前 50 个）",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。"""
    data = data or {}
    params = params or {}
    try:
        ak = _field(data, "secret_id")
        sk = _field(data, "secret_key")
        if not ak or not sk:
            return {"ok": False, "error": "SecretId 或 SecretKey 为空"}

        if action_id == "whoami":
            return _run_whoami(ak, sk)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, params)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, params)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def _run_whoami(ak: str, sk: str) -> dict:
    """身份信息 + 权限探测（各接口独立只读调用）。"""
    with make_client() as client:
        resp = _tc3_call(
            client, STS_HOST, STS_SERVICE, STS_ACTION, STS_VERSION, STS_REGION, {}, ak, sk
        )
        body = safe_json(resp) or {}
        response_body = body.get("Response")
        response_body = response_body if isinstance(response_body, dict) else {}
        if resp.status_code != 200 or isinstance(response_body.get("Error"), dict):
            return {"ok": False, "error": _tc3_failure(resp, ak, sk)}

        rows = [
            {"item": "账号 ID", "value": response_body.get("AccountId") or "—"},
            {"item": "ARN", "value": response_body.get("Arn") or "—"},
            {"item": "PrincipalId", "value": response_body.get("PrincipalId") or "—"},
        ]

        rights = []
        if _tc3_probe(
            client,
            CVM_HOST,
            CVM_SERVICE,
            "DescribeInstances",
            CVM_VERSION,
            DEFAULT_REGION,
            {"Limit": 1},
            ak,
            sk,
        ) == _PROBE_OK:
            rights.append("云主机管理")
        if _cos_probe_buckets(client, ak, sk) == _PROBE_OK:
            rights.append("对象存储")
        rows.append({"item": "探测到的权限", "value": "、".join(rights) if rights else "未探测到额外资源权限"})

    return {"ok": True, "rows": rows, "columns": _WHOAMI_COLUMNS, "total": len(rows), "has_more": False}


def _run_list_instances(ak: str, sk: str, params: dict) -> dict:
    region = _field(params, "region") or DEFAULT_REGION
    if not _REGION_RE.match(region):
        return {"ok": False, "error": "地域不合法"}
    with make_client() as client:
        resp = _tc3_call(
            client,
            CVM_HOST,
            CVM_SERVICE,
            "DescribeInstances",
            CVM_VERSION,
            region,
            {"Limit": 100},
            ak,
            sk,
        )
    body = safe_json(resp) or {}
    response_body = body.get("Response")
    response_body = response_body if isinstance(response_body, dict) else {}
    if resp.status_code != 200 or isinstance(response_body.get("Error"), dict):
        return {"ok": False, "error": _tc3_failure(resp, ak, sk)}

    instances = response_body.get("InstanceSet")
    instances = instances if isinstance(instances, list) else []
    rows = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        ips = inst.get("PublicIpAddresses")
        ips = ips if isinstance(ips, list) else []
        rows.append(
            {
                "InstanceId": inst.get("InstanceId") or "",
                "InstanceName": inst.get("InstanceName") or "",
                "InstanceState": inst.get("InstanceState") or "",
                "PublicIp": ips[0] if ips else "",
                "InstanceType": inst.get("InstanceType") or "",
            }
        )
    total = int(response_body.get("TotalCount") or len(rows))
    return {
        "ok": True,
        "rows": rows,
        "columns": _INSTANCE_COLUMNS,
        "total": total,
        "empty_text": f"暂无云主机实例（地域 {region}）",
        "has_more": total > len(rows),
    }


def _run_list_buckets(ak: str, sk: str) -> dict:
    with make_client() as client:
        resp = _cos_get(client, COS_GLOBAL_HOST, "/", {}, ak, sk)
    if resp.status_code != 200:
        return {"ok": False, "error": _cos_failure(resp, ak, sk)}

    rows = []
    for bucket in _xml_findall(resp.text, "Bucket"):
        name = _xml_text(bucket, "Name")
        if not name:
            continue
        region = _xml_text(bucket, "Location") or DEFAULT_REGION
        rows.append(
            {
                "name": name,
                "creationDate": _xml_text(bucket, "CreationDate"),
                "region": region,
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _BUCKET_COLUMNS,
        "row_actions": [_OBJECT_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
    }


def _run_list_objects(ak: str, sk: str, params: dict) -> dict:
    bucket = _field(params, "bucket")
    if not bucket:
        return {
            "ok": False,
            "error": "缺少桶名，请在表单里填写，或从「对象存储桶列表」行内点击「预览」进入",
        }
    if not _BUCKET_RE.match(bucket):
        return {"ok": False, "error": "桶名不合法"}

    region = _field(params, "region") or DEFAULT_REGION
    if not _REGION_RE.match(region):
        return {"ok": False, "error": "地域不合法"}

    host = f"{bucket}.cos.{region}.myqcloud.com"
    with make_client() as client:
        resp = _cos_get(client, host, "/", {"max-keys": "50"}, ak, sk)
    if resp.status_code != 200:
        return {"ok": False, "error": _cos_failure(resp, ak, sk)}

    root = _xml_root(resp.text)
    rows = []
    for content in _xml_findall(root, "Contents"):
        key = _xml_text(content, "Key")
        if not key:
            continue
        size_raw = _xml_text(content, "Size")
        rows.append(
            {
                "key": key,
                "size": int(size_raw) if size_raw.isdigit() else size_raw,
                "lastModified": _xml_text(content, "LastModified"),
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _OBJECT_COLUMNS,
        "total": len(rows),
        "empty_text": f"该桶暂无对象（{bucket}，仅显示前 50 个）",
        "has_more": _xml_text(root, "IsTruncated").lower() == "true",
    }


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    return "腾讯云 | SecretId/SecretKey"
