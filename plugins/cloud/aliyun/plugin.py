"""阿里云 AccessKey 验证插件（plugins/cloud/ 类别下的独立子插件）。

自带全部实现：**两套签名**都在本文件内维护，不依赖类别共享模块：

- RPC 签名 v1（HMAC-SHA1）——STS / ECS 等云 API 走这套；
- OSS 签名 v1（``Authorization: OSS <AK>:<Signature>``）——对象存储走这套，
  规则与云 API 完全不同，故单独实现。

全部调用均为只读，不产生任何资源变更。仅从框架层 backend.plugin_sdk 复用通用工具。

本插件提供的操作（``actions``）：

- ``whoami``           查看调用者身份（STS GetCallerIdentity + 权限探测）
- ``list_regions``     地域列表（ECS DescribeRegions，含中文名；行内「实例」下钻）
- ``list_instances``   云主机实例列表（ECS DescribeInstances，表单可改地域）
- ``list_buckets``     对象存储桶列表（OSS ListBuckets；桶列表自带地域）
- ``list_objects``     对象预览（OSS ListObjectsV2 + delimiter，文件与文件夹分行）
- ``download_object``  下载单个对象（``save_to``，前端弹「另存为」）
- ``download_prefix``  把某个前缀（文件夹）下的对象并发拉取后打包成 zip 下载

下载类操作全部走签名 GET 流式读取，磁盘上限见 ``MAX_ZIP_*``。
两个下载操作都声明了 ``progress``，由 ``stream_action`` 边下边回调进度、并响应取消
（``run_action`` 三个参数调用时不上报也不可取消，行为与以前一致）。

参考文档：https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-getcalleridentity
参考文档：https://help.aliyun.com/zh/oss/developer-reference/include-signatures-in-the-authorization-header
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Any
from urllib.parse import quote

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    download_to_file,
    first_list,
    guarded,
    make_client,
    safe_json,
)

# 阿里云 STS RPC 接口地址（公共参数走查询串，GET 请求）
STS_ENDPOINT = "https://sts.aliyuncs.com"

# ECS（云主机）RPC 接口，与 STS 共用同一套 RPC 签名 v1
ECS_ENDPOINT = "https://ecs.aliyuncs.com"
ECS_VERSION = "2014-05-26"

# OSS（对象存储）全局端点：ListBuckets 走这里
OSS_GLOBAL_ENDPOINT = "https://oss.aliyuncs.com"

# 默认地域（ECS / OSS 均先按此探测）
DEFAULT_REGION = "cn-hangzhou"

# 对象预览每页条数；枚举（打包下载）时按 OSS 单页上限翻页
_PAGE_SIZE = 50
_ENUM_PAGE = 1000

# 打包下载上限：**宁可明确报错，也不静默截断**（前端会原样展示 error/output）
MAX_ZIP_OBJECTS = 1000                # 单次最多打包的对象数
MAX_ZIP_BYTES = 512 * 1024 * 1024     # 单次最多打包的原始总字节数（512 MiB）
_ZIP_WORKERS = 8                      # 并发拉取对象的线程数

# 下载进度上报的节流：每写入这么多字节、或隔这么久才回调一次
# （chunk 是 64 KiB，大文件不动节流会打出几万次事件）
_PROGRESS_STEP = 256 * 1024           # 256 KiB
_PROGRESS_INTERVAL = 0.2              # 秒

# 权限探测的单请求超时（秒）——探测要「整体十几秒内」结束，不能用默认的 20 秒 ×3
_PROBE_TIMEOUT = 6.0

# 认证失败错误码（其余一律按「无法确认」处理）
_INVALID_CODES = {
    "InvalidAccessKeyId.NotFound",
    "InvalidAccessKeyId.Inactive",
    "SignatureDoesNotMatch",
    "InvalidAccessKeyId",
}

# 权限探测结论：ok / denied（明确无权限）/ bad_cred（凭据被拒）/ unknown（拿不准 → fail-open）
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

# RPC 错误码 → 结论
_RPC_DENIED_CODES = {
    "Forbidden",
    "Forbidden.RAM",
    "Forbidden.UserNotAuthorized",
    "Forbidden.AccountAccess",
    "NoPermission",
    "NoPermission.User",
}

# OSS 错误码 → 结论
_OSS_BAD_CRED_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidAccessKeyId.NotFound"}

# 桶名 / 端点白名单式校验：避免把用户/接口返回的字符串直接拼进 URL
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")
_HOST_RE = re.compile(r"^[a-z0-9.\-]+$")


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


# ── OSS 签名 v1（与云 API 的 RPC 签名无关，独立实现）─────────

def _oss_signature(
    method: str,
    content_md5: str,
    content_type: str,
    date: str,
    canonicalized_oss_headers: str,
    canonicalized_resource: str,
    secret: str,
) -> str:
    """OSS 签名 v1：base64(HMAC-SHA1(Secret, StringToSign))。

    StringToSign = VERB\\nContent-MD5\\nContent-Type\\nDate\\n
                   CanonicalizedOSSHeaders + CanonicalizedResource
    """
    string_to_sign = (
        f"{method}\n{content_md5}\n{content_type}\n{date}\n"
        f"{canonicalized_oss_headers}{canonicalized_resource}"
    )
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _oss_auth_headers(
    access_key_id: str, access_key_secret: str, canonicalized_resource: str
) -> dict:
    """一次 OSS V1 签名所需的请求头（无 x-oss-*，故 CanonicalizedOSSHeaders 为空）。

    流式下载（``client.stream``）与普通 GET 共用，避免签名逻辑写两遍。
    """
    date = formatdate(usegmt=True)
    signature = _oss_signature(
        "GET", "", "", date, "", canonicalized_resource, access_key_secret
    )
    return {"Date": date, "Authorization": f"OSS {access_key_id}:{signature}"}


def _oss_get(
    client,
    url: str,
    access_key_id: str,
    access_key_secret: str,
    canonicalized_resource: str,
    params: dict | None = None,
    timeout: float | None = None,
):
    """发起一次 OSS V1 签名的只读 GET。"""
    headers = _oss_auth_headers(access_key_id, access_key_secret, canonicalized_resource)
    kwargs: dict[str, Any] = {"params": params, "headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(url, **kwargs)


# ── XML 解析（OSS 返回 XML；用本地名匹配，绕开 namespace 差异）──

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
    """取某个后代元素的文本（第一个）。"""
    if elem is None:
        return ""
    for child in elem.iter():
        if _local_name(child.tag) == local_name:
            return (child.text or "").strip()
    return ""


def _xml_error(text: str) -> tuple[str, str]:
    """OSS 错误响应 <Error><Code/><Message/></Error> → (code, message)。"""
    root = _xml_root(text)
    if root is None:
        return "", ""
    return _xml_text(root, "Code"), _xml_text(root, "Message")


# ── 权限探测（只读；拿不准 → fail-open）─────────────────

def _rpc_probe(client, endpoint, action, version, extra, ak, sk) -> str:
    """探测一次 RPC 接口，返回结论字符串；异常一律 unknown（不抛出）。"""
    try:
        resp = _rpc_get(client, endpoint, action, version, extra, ak, sk, _PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 - 探测失败不能让整个 capabilities 崩
        return _PROBE_UNKNOWN
    payload = safe_json(resp) or {}
    code = str(payload.get("Code") or "").strip()
    if resp.status_code == 200 and not code:
        return _PROBE_OK
    if code in _INVALID_CODES:
        return _PROBE_BAD_CRED
    if code in _RPC_DENIED_CODES or code.startswith("Forbidden") or code.startswith("NoPermission"):
        return _PROBE_DENIED
    if resp.status_code == 403 and not code:
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _oss_probe_buckets(client, ak, sk) -> str:
    """探测 OSS ListBuckets（只读），返回结论字符串。"""
    try:
        resp = _oss_get(
            client, f"{OSS_GLOBAL_ENDPOINT}/", ak, sk, "/", timeout=_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return _PROBE_UNKNOWN
    if resp.status_code == 200:
        return _PROBE_OK
    code, _ = _xml_error(resp.text)
    if code in _OSS_BAD_CRED_CODES:
        return _PROBE_BAD_CRED
    if code in ("AccessDenied", "UserDisable", "Forbidden"):
        return _PROBE_DENIED
    return _PROBE_UNKNOWN


def _probe_all(ak: str, sk: str) -> dict:
    """依次只读探测 STS / ECS / OSS，返回 {action_id: 结论}（彼此独立）。"""
    status: dict[str, str] = {}
    with make_client() as client:
        status["whoami"] = _rpc_probe(
            client, STS_ENDPOINT, "GetCallerIdentity", "2015-04-01", {}, ak, sk
        )
        status["list_regions"] = _rpc_probe(
            client, ECS_ENDPOINT, "DescribeRegions", ECS_VERSION, {}, ak, sk
        )
        status["list_instances"] = _rpc_probe(
            client,
            ECS_ENDPOINT,
            "DescribeInstances",
            ECS_VERSION,
            {"RegionId": DEFAULT_REGION, "PageSize": "1"},
            ak,
            sk,
        )
        status["list_buckets"] = _oss_probe_buckets(client, ak, sk)
    # 对象预览 / 单对象下载 / 打包下载与桶列表是同一个 OSS 权限位
    status["list_objects"] = status["list_buckets"]
    status["download_object"] = status["list_buckets"]
    status["download_prefix"] = status["list_buckets"]
    return status


def capabilities(data: dict) -> dict:
    """返回当前凭据**真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    探测 STS / ECS / OSS 三个只读接口，各自独立 try：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 凭据被拒」→ 隐藏该操作；
    - 网络异常或返回看不懂 → **unknown**，按可用处理（fail-open）。

    若三个接口全都拿不准，则直接抛异常，交给框架按「未知 → 全部可用」兜底。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "access_key_secret")
    if not ak or not sk:
        return {"available": [], "note": "缺少 AccessKey ID 或 AccessKey Secret"}

    status = _probe_all(ak, sk)
    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定 —— 让框架按「未知」处理，绝不返回空列表把操作全隐藏
        raise RuntimeError("阿里云权限探测全部失败（网络异常或响应无法识别）")

    if status.get("whoami") == _PROBE_BAD_CRED:
        # 基础身份接口都明确拒了这个 AK —— 同一对密钥在 ECS/OSS 上必然也被拒，
        # 此时剩下几个探针的 unknown 不是「拿不准」而是「密钥已废」，没必要假装可用。
        return {"available": [], "note": "AccessKey 无效或已禁用，任何操作都会失败"}

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

    with make_client() as client:
        resp = _rpc_get(
            client, STS_ENDPOINT, "GetCallerIdentity", "2015-04-01", {}, access_key_id, access_key_secret
        )

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


# ── 通用请求封装 ─────────────────────────────────────────

def _rpc_get(client, endpoint, action, version, extra, ak, sk, timeout: float | None = None):
    """发起一次 RPC 签名 v1 的只读 GET 请求。"""
    params: dict[str, Any] = {
        "Format": "JSON",
        "Version": version,
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Action": action,
    }
    params.update(extra or {})
    params["Signature"] = _aliyun_signature(params, sk)
    kwargs: dict[str, Any] = {"params": params}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client.get(endpoint, **kwargs)


def _oss_region(location: str) -> str:
    """把 OSS 的 Location（如 ``oss-cn-hangzhou``）归一成地域名 ``cn-hangzhou``。"""
    value = (location or "").strip()
    if value.startswith("oss-"):
        value = value[4:]
    return value or DEFAULT_REGION


def _oss_endpoint(region: str) -> str:
    return f"oss-{region}.aliyuncs.com"


# ── 操作 ────────────────────────────────────────────────

def _redact(text: str, *secrets: str) -> str:
    """把可能出现在接口返回中的密钥替换掉，任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _rpc_failure(resp, ak: str, sk: str) -> str:
    """把 RPC 失败响应转成一句脱敏后的错误信息。"""
    payload = safe_json(resp) or {}
    code = str(payload.get("Code") or "").strip()
    detail = str(payload.get("Message") or code or f"HTTP {resp.status_code}")
    return f"请求失败：{_redact(detail, ak, sk)}"


def _oss_failure(resp, ak: str, sk: str) -> str:
    """把 OSS 失败响应（XML）转成一句脱敏后的错误信息。"""
    code, message = _xml_error(resp.text)
    detail = message or code or f"HTTP {resp.status_code}"
    return f"请求失败：{_redact(detail, ak, sk)}"


_WHOAMI_COLUMNS = [
    {"key": "item", "label": "项目", "width": 140},
    {"key": "value", "label": "值", "mono": True},
]

_REGION_COLUMNS = [
    {"key": "RegionId", "label": "地域 ID", "mono": True},
    {"key": "LocalName", "label": "名称"},
    {"key": "RegionEndpoint", "label": "接入点", "width": 220, "mono": True},
]

_INSTANCE_COLUMNS = [
    {"key": "InstanceId", "label": "实例 ID", "mono": True},
    {"key": "InstanceName", "label": "实例名"},
    {"key": "Status", "label": "状态", "width": 100, "type": "tag"},
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
    {"key": "type", "label": "类型", "width": 80, "type": "tag"},
    {"key": "size", "label": "大小", "width": 100, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

# 地域列表行内「实例」下钻到 list_instances（region 作为 params 传下去预填表单）
_REGION_ROW_ACTION = {
    "id": "list_instances",
    "label": "实例",
    "params": {"region": "$row.RegionId"},
}

# 桶列表行内「预览」下钻到 list_objects
_OBJECT_ROW_ACTION = {
    "id": "list_objects",
    "label": "预览",
    "params": {"bucket": "$row.name", "region": "$row.region", "endpoint": "$row.endpoint"},
}

# 对象列表的行内按钮：文件夹行有 folderPath（「进入」「打包下载」可用），
# 文件行有 fileKey（「下载」可用）—— 对方没有的键由前端自动置灰，绝不会传 undefined 过来。
_OBJECT_ROW_ACTIONS = [
    {
        "id": "list_objects",
        "label": "进入",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "endpoint": "$row.endpoint",
            "prefix": "$row.folderPath",
        },
    },
    {
        "id": "download_object",
        "label": "下载",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "endpoint": "$row.endpoint",
            "key": "$row.fileKey",
        },
    },
    {
        "id": "download_prefix",
        "label": "打包下载",
        "params": {
            "bucket": "$row.bucket",
            "region": "$row.region",
            "endpoint": "$row.endpoint",
            "prefix": "$row.folderPath",
        },
    },
]

_BUCKET_FIELD = {"key": "bucket", "label": "桶名", "placeholder": "如 my-bucket"}
_REGION_FIELD = {
    "key": "region",
    "label": "地域",
    "placeholder": f"默认 {DEFAULT_REGION}，如 cn-beijing",
}
_ENDPOINT_FIELD = {
    "key": "endpoint",
    "label": "Endpoint",
    "placeholder": "留空按地域推导，如 oss-cn-hangzhou.aliyuncs.com",
}


def _object_row_file(
    bucket: str, region: str, endpoint: str, key: str, size_raw: str, modified: str
) -> dict:
    """文件行（fileKey / fileName 只出现在文件行，供「下载」与另存为默认名使用）。"""
    return {
        "key": key,
        "type": "文件",
        "size": int(size_raw) if str(size_raw).isdigit() else size_raw,
        "lastModified": modified,
        "bucket": bucket,
        "region": region,
        "endpoint": endpoint,
        "fileKey": key,
        "fileName": key.rstrip("/").rsplit("/", 1)[-1],
    }


def _object_row_folder(bucket: str, region: str, endpoint: str, prefix: str) -> dict:
    """文件夹行（folderPath / folderName 只出现在文件夹行）。"""
    name = prefix.rstrip("/").rsplit("/", 1)[-1] or "bucket"
    return {
        "key": prefix,
        "type": "文件夹",
        "size": "",
        "lastModified": "",
        "bucket": bucket,
        "region": region,
        "endpoint": endpoint,
        "folderPath": prefix,
        "folderName": f"{name}.zip",
    }


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的身份 / 实例 / 对象存储查询）。"""
    data = data or {}
    if not (_field(data, "access_key_id") and _field(data, "access_key_secret")):
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
            "id": "list_regions",
            "name": "地域列表",
            "kind": "list",
            "group": "计算",
            "order": 15,
            "description": "ECS DescribeRegions（列出该账号可选地域，行内「实例」下钻到该地域的云主机）",
            "columns": _REGION_COLUMNS,
            "row_actions": [_REGION_ROW_ACTION],
            "empty_text": "未获取到地域列表",
        },
        {
            "id": "list_instances",
            "name": "云主机实例列表",
            "kind": "list",
            "group": "计算",
            "order": 20,
            "description": f"ECS DescribeInstances（地域默认 {DEFAULT_REGION}，可直接填，或从「地域列表」行内进入）",
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「刷新」时并入参数
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "empty_text": f"暂无云主机实例（默认地域 {DEFAULT_REGION}）",
        },
        {
            "id": "list_buckets",
            "name": "对象存储桶列表",
            "kind": "list",
            "group": "存储",
            "order": 30,
            "description": "OSS ListBuckets（只读；桶自带地域，无需先选地域）",
            "columns": _BUCKET_COLUMNS,
            "row_actions": [_OBJECT_ROW_ACTION],
            "empty_text": "暂无对象存储桶",
        },
        {
            "id": "list_objects",
            "name": "对象浏览",
            "kind": "list",
            "group": "存储",
            "order": 40,
            "description": "OSS ListObjectsV2（按 / 分隔列出文件与文件夹；行内可「进入」「下载」「打包下载」）",
            "columns": _OBJECT_COLUMNS,
            "fields": [
                _BUCKET_FIELD,
                {
                    "key": "prefix",
                    "label": "前缀",
                    "placeholder": "留空为桶根目录，如 dir/",
                },
                _REGION_FIELD,
                _ENDPOINT_FIELD,
            ],
            "row_actions": _OBJECT_ROW_ACTIONS,
            "submit": "查询",
            "empty_text": "该前缀下暂无对象（仅显示前 50 个）",
        },
        {
            "id": "download_object",
            "name": "下载对象",
            "kind": "form",
            "group": "存储",
            "order": 50,
            "description": "下载桶内的单个对象（弹「另存为」；从对象列表行内「下载」进入会自动填好）",
            "fields": [
                _BUCKET_FIELD,
                {"key": "key", "label": "对象 Key", "required": True, "placeholder": "如 dir/file.txt"},
                _REGION_FIELD,
                _ENDPOINT_FIELD,
            ],
            "save_to": {"key": "path", "default_name": "$row.fileName"},
            "progress": True,
            "submit": "下载",
        },
        {
            "id": "download_prefix",
            "name": "打包下载（zip）",
            "kind": "form",
            "group": "存储",
            "order": 60,
            "description": (
                f"把该前缀下所有对象拉下来打成一个 zip（最多 {MAX_ZIP_OBJECTS} 个对象 / "
                f"{_human_size(MAX_ZIP_BYTES)}，超出会明确报错而不静默截断）"
            ),
            "fields": [
                _BUCKET_FIELD,
                {"key": "prefix", "label": "前缀（文件夹）", "placeholder": "如 dir/sub/"},
                _REGION_FIELD,
                _ENDPOINT_FIELD,
            ],
            "save_to": {"key": "path", "default_name": "$row.folderName"},
            "progress": True,
            "submit": "打包下载",
        },
    ]


def run_action(action_id: str, params: dict, data: dict, progress=None, should_cancel=None) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。

    ``progress`` / ``should_cancel`` 都是**可选**的下载钩子（见 ``stream_action``）：

    - ``progress(current, total, text)``：上报进度，``total <= 0`` 表示总大小未知；
    - ``should_cancel() -> bool``：返回 True 表示用户点了「取消」，下载循环会尽快干净地停下。

    只有 ``stream_action`` 会传它们，框架直接调用 ``run_action`` 时保持三个参数不变。
    """
    data = data or {}
    params = params or {}
    try:
        ak = _field(data, "access_key_id")
        sk = _field(data, "access_key_secret")
        if not ak or not sk:
            return {"ok": False, "error": "AccessKey ID 或 AccessKey Secret 为空"}

        if action_id == "whoami":
            return _run_whoami(ak, sk)
        if action_id == "list_regions":
            return _run_list_regions(ak, sk)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, params)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, params)
        if action_id == "download_object":
            return _run_download_object(ak, sk, params, progress, should_cancel)
        if action_id == "download_prefix":
            return _run_download_prefix(ak, sk, params, progress, should_cancel)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def stream_action(
    action_id: str,
    params: dict,
    data: dict,
    on_delta,
    on_progress=None,
    should_cancel=None,
) -> dict:
    """下载类操作的流式执行：边下边上报进度、并响应取消。其它操作回退到 run_action。

    ``on_progress(current, total, text)`` 与 ``should_cancel()`` 都由框架按关键字传入
    （写进签名才会传），都为 None 时下载路径与 ``run_action`` 完全一致。
    """
    if action_id in ("download_object", "download_prefix"):
        return run_action(action_id, params, data, on_progress, should_cancel)
    return run_action(action_id, params, data)


def _run_whoami(ak: str, sk: str) -> dict:
    """身份信息 + 权限探测（各接口独立只读调用）。"""
    with make_client() as client:
        resp = _rpc_get(client, STS_ENDPOINT, "GetCallerIdentity", "2015-04-01", {}, ak, sk)
        payload = safe_json(resp) or {}
        code = str(payload.get("Code") or "").strip()
        if resp.status_code != 200 or code:
            return {"ok": False, "error": _rpc_failure(resp, ak, sk)}

        rows = [
            {"item": "账号 ID", "value": payload.get("AccountId") or "—"},
            {"item": "ARN", "value": payload.get("Arn") or "—"},
            {"item": "UserId", "value": payload.get("UserId") or "—"},
        ]

        rights = []
        if _rpc_probe(
            client,
            ECS_ENDPOINT,
            "DescribeInstances",
            ECS_VERSION,
            {"RegionId": DEFAULT_REGION, "PageSize": "1"},
            ak,
            sk,
        ) == _PROBE_OK:
            rights.append("云主机管理")
        if _oss_probe_buckets(client, ak, sk) == _PROBE_OK:
            rights.append("对象存储")
        rows.append({"item": "探测到的权限", "value": "、".join(rights) if rights else "未探测到额外资源权限"})

    return {"ok": True, "rows": rows, "columns": _WHOAMI_COLUMNS, "total": len(rows), "has_more": False}


def _run_list_regions(ak: str, sk: str) -> dict:
    """地域列表（ECS DescribeRegions，只读）：Regions.Region[] → 地域 ID / 中文名。"""
    with make_client() as client:
        resp = _rpc_get(client, ECS_ENDPOINT, "DescribeRegions", ECS_VERSION, {}, ak, sk)
    payload = safe_json(resp) or {}
    if resp.status_code != 200 or payload.get("Code"):
        return {"ok": False, "error": _rpc_failure(resp, ak, sk)}

    regions = first_list(payload, "Regions", "Region") or []
    rows = []
    for item in regions:
        if not isinstance(item, dict):
            continue
        region_id = item.get("RegionId") or ""
        if not region_id:
            continue
        rows.append(
            {
                "RegionId": region_id,
                "LocalName": item.get("LocalName") or "",
                "RegionEndpoint": item.get("RegionEndpoint") or "",
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _REGION_COLUMNS,
        "row_actions": [_REGION_ROW_ACTION],
        "total": len(rows),
        "has_more": False,
        "empty_text": "未获取到地域列表",
    }


def _run_list_instances(ak: str, sk: str, params: dict) -> dict:
    region = _field(params, "region") or DEFAULT_REGION
    with make_client() as client:
        resp = _rpc_get(
            client,
            ECS_ENDPOINT,
            "DescribeInstances",
            ECS_VERSION,
            {"RegionId": region, "PageSize": "100"},
            ak,
            sk,
        )
    payload = safe_json(resp) or {}
    if resp.status_code != 200 or payload.get("Code"):
        return {"ok": False, "error": _rpc_failure(resp, ak, sk)}

    instances = first_list(payload, "Instances") or []
    rows = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        ips = first_list(inst.get("PublicIpAddress") or {}, "IpAddress") or []
        rows.append(
            {
                "InstanceId": inst.get("InstanceId") or "",
                "InstanceName": inst.get("InstanceName") or "",
                "Status": inst.get("Status") or "",
                "PublicIp": ips[0] if ips else "",
                "InstanceType": inst.get("InstanceType") or "",
            }
        )
    return {
        "ok": True,
        "rows": rows,
        "columns": _INSTANCE_COLUMNS,
        "total": int(payload.get("TotalCount") or len(rows)),
        "empty_text": f"暂无云主机实例（地域 {region}）",
        "has_more": bool(payload.get("TotalCount") and int(payload["TotalCount"]) > len(rows)),
    }


def _run_list_buckets(ak: str, sk: str) -> dict:
    with make_client() as client:
        resp = _oss_get(client, f"{OSS_GLOBAL_ENDPOINT}/", ak, sk, "/")
    if resp.status_code != 200:
        return {"ok": False, "error": _oss_failure(resp, ak, sk)}

    rows = []
    for bucket in _xml_findall(resp.text, "Bucket"):
        name = _xml_text(bucket, "Name")
        if not name:
            continue
        region = _oss_region(_xml_text(bucket, "Location"))
        endpoint = _xml_text(bucket, "ExtranetEndpoint") or _oss_endpoint(region)
        rows.append(
            {
                "name": name,
                "creationDate": _xml_text(bucket, "CreationDate"),
                "region": region,
                # endpoint 不展示为列，仅供行内「预览」下钻时作为参数使用
                "endpoint": endpoint,
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


def _oss_target(params: dict) -> tuple[dict, str]:
    """校验并归一化 OSS 目标（桶名 / 地域 / endpoint），返回 ``(target, error)``。

    桶名与 endpoint 都做过白名单式校验，避免把接口/用户给的字符串直接拼进 URL。
    """
    bucket = _field(params, "bucket")
    if not bucket:
        return {}, "缺少桶名，请在表单里填写，或从「对象存储桶列表」行内点击「预览」进入"
    if not _BUCKET_RE.match(bucket):
        return {}, "桶名不合法"
    region = _oss_region(_field(params, "region"))
    endpoint = _field(params, "endpoint") or _oss_endpoint(region)
    if not _HOST_RE.match(endpoint):
        return {}, "对象存储端点不合法"
    return {"bucket": bucket, "region": region, "endpoint": endpoint}, ""


def _run_list_objects(ak: str, sk: str, params: dict) -> dict:
    """列出一个前缀下的对象与子目录（ListObjectsV2 + delimiter=/）。"""
    target, error = _oss_target(params)
    if error:
        return {"ok": False, "error": error}
    bucket = target["bucket"]
    endpoint = target["endpoint"]
    prefix = _field(params, "prefix")

    url = f"https://{bucket}.{endpoint}/"
    query: dict[str, str] = {"list-type": "2", "delimiter": "/", "max-keys": str(_PAGE_SIZE)}
    if prefix:
        query["prefix"] = prefix
    with make_client() as client:
        resp = _oss_get(client, url, ak, sk, f"/{bucket}/", params=query)
    if resp.status_code != 200:
        return {"ok": False, "error": _oss_failure(resp, ak, sk)}

    root = _xml_root(resp.text)
    rows: list[dict] = []
    # CommonPrefixes 即「子目录」，排在文件前面
    for node in _xml_findall(root, "CommonPrefixes"):
        child = _xml_text(node, "Prefix")
        if child:
            rows.append(_object_row_folder(bucket, target["region"], endpoint, child))
    for content in _xml_findall(root, "Contents"):
        key = _xml_text(content, "Key")
        if not key or key.endswith("/"):     # 目录占位对象：已由 CommonPrefixes 表达
            continue
        rows.append(
            _object_row_file(
                bucket,
                target["region"],
                endpoint,
                key,
                _xml_text(content, "Size"),
                _xml_text(content, "LastModified"),
            )
        )
    where = prefix or "桶根目录"
    return {
        "ok": True,
        "rows": rows,
        "columns": _OBJECT_COLUMNS,
        "row_actions": _OBJECT_ROW_ACTIONS,
        "total": len(rows),
        "empty_text": f"该前缀下暂无对象（{bucket} / {where}，仅显示前 {_PAGE_SIZE} 个）",
        "has_more": _xml_text(root, "IsTruncated").lower() == "true",
    }


class _Cancelled(Exception):
    """协作式取消（用户点了「取消」）：内部信号，只在插件内部传播，不会逃出边界。"""


def _remove_quietly(path: str) -> None:
    """删除临时文件；不存在 / 删不掉都不抛（清理不能盖过真正的失败原因）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _content_length(response) -> int:
    """响应的 ``Content-Length``；拿不到 / 非法 / 非正数一律返回 0（= 总大小未知）。"""
    try:
        raw = response.headers.get("Content-Length") or ""
        value = int(str(raw).strip())
    except Exception:  # noqa: BLE001 - 头缺失或不是数字都只是「不知道总大小」
        return 0
    return value if value > 0 else 0


def _download_with_progress(
    response, path: str, key: str, progress=None, should_cancel=None
) -> tuple[bool, str]:
    """带进度上报 / 取消响应的流式落盘（``.part`` + 原子替换语义与 ``download_to_file`` 一致）。

    **只在调用方拿到 ``progress`` 或 ``should_cancel`` 时才走这里**：两个钩子都没有时仍调用
    ``download_to_file``，保证「不上报也不可取消」路径的行为与以前逐字节一致。

    取消时删掉 ``.part`` 后返回 ``(False, "下载已取消")``——目标路径始终没被写过。
    """
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    total = _content_length(response)

    def _emit(current: int, text: str) -> None:
        """上报一次进度；没接回调（例如只传了 should_cancel）时静默。"""
        if progress is not None:
            progress(current, total, text)

    def _amount(current: int) -> str:
        """给用户看的量：``12.3 MB / 45.0 MB``；总大小未知（total<=0）时只写已下载量。"""
        if total > 0:
            return f"{_human_size(current)} / {_human_size(total)}"
        return _human_size(current)

    _emit(0, f"开始下载 {key.rsplit('/', 1)[-1] or key}")
    tmp = target + ".part"
    written = 0
    reported = 0
    last = time.monotonic()
    try:
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes(65536):
                if should_cancel is not None and should_cancel():
                    raise _Cancelled
                handle.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                # 节流：攒够 256 KiB 或过了 0.2 秒才回调一次
                if written - reported >= _PROGRESS_STEP or now - last >= _PROGRESS_INTERVAL:
                    reported, last = written, now
                    _emit(written, _amount(written))
        os.replace(tmp, target)      # 同目录内的替换是原子的
    except _Cancelled:
        _remove_quietly(tmp)
        return False, "下载已取消"
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        _remove_quietly(tmp)
        return False, f"写入失败：{type(exc).__name__}"
    if written != reported:          # 不足一个节流阈值的小文件也补报终值，别让进度条停在 0
        _emit(written, _amount(written))
    return True, target


def _run_download_object(
    ak: str, sk: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """下载单个对象（流式写入 save_to 指定的路径）；给了钩子就边写边上报 / 响应取消。"""
    target, error = _oss_target(params)
    if error:
        return {"ok": False, "error": error}
    key = _field(params, "key")
    if not key:
        return {"ok": False, "error": "缺少对象 Key"}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}

    bucket = target["bucket"]
    url = f"https://{bucket}.{target['endpoint']}/{quote(key, safe='/~')}"
    headers = _oss_auth_headers(ak, sk, f"/{bucket}/{key}")
    with make_client() as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()                       # 错误体很小，读出来好给出原因
                return {"ok": False, "error": _oss_failure(resp, ak, sk)}
            if progress is None and should_cancel is None:
                ok, message = download_to_file(resp, path)
            else:
                ok, message = _download_with_progress(resp, path, key, progress, should_cancel)
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "output": f"已保存到 {message}"}


def _list_all_objects(
    client, bucket: str, endpoint: str, ak: str, sk: str, prefix: str, limit: int
) -> tuple[list[tuple[str, int]], bool, str]:
    """翻页枚举前缀下的**全部**对象（打包下载用，不受预览的 50 条限制）。

    返回 ``(objects, over_limit, error)``；``over_limit`` 为真表示已超过 ``limit`` 个
    并提前停止（调用方据此明确报错，不做静默截断）。
    """
    url = f"https://{bucket}.{endpoint}/"
    token = ""
    objects: list[tuple[str, int]] = []
    while True:
        query: dict[str, str] = {"list-type": "2", "max-keys": str(_ENUM_PAGE)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        resp = _oss_get(client, url, ak, sk, f"/{bucket}/", params=query)
        if resp.status_code != 200:
            return objects, False, _oss_failure(resp, ak, sk)
        root = _xml_root(resp.text)
        for content in _xml_findall(root, "Contents"):
            key = _xml_text(content, "Key")
            if not key or key.endswith("/"):
                continue
            size_raw = _xml_text(content, "Size")
            objects.append((key, int(size_raw) if size_raw.isdigit() else 0))
            if len(objects) > limit:
                return objects, True, ""
        if _xml_text(root, "IsTruncated").lower() != "true":
            break
        token = _xml_text(root, "NextContinuationToken")
        if not token:
            break
    return objects, False, ""


def _zip_entry_name(key: str, prefix: str) -> str | None:
    """zip 内的相对路径：去掉前缀、保留目录结构；路径不安全（.. / 绝对路径）时返回 None。"""
    rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
    rel = rel.lstrip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        return None
    return rel


def _run_download_prefix(
    ak: str, sk: str, params: dict, progress=None, should_cancel=None
) -> dict:
    """把某个前缀下的所有对象并发拉取后打包成 zip（本地临时文件 → 原子落盘）。

    给了 ``progress`` 时按**对象个数**上报（跨对象累加字节数对用户不直观）；
    给了 ``should_cancel`` 时在**每个对象开始前**和**对象内的每个 chunk** 各查一次
    （正在下的那个大对象也立刻断流），取消即停止后续下载并清干净临时文件。
    """
    target, error = _oss_target(params)
    if error:
        return {"ok": False, "error": error}
    path = _field(params, "path")
    if not path:
        return {"ok": False, "error": "未指定保存路径"}
    prefix = _field(params, "prefix")
    if not prefix:
        return {"ok": False, "error": "缺少前缀（文件夹），请在表单里填写，或从对象列表行内点击「打包下载」进入"}

    bucket = target["bucket"]
    endpoint = target["endpoint"]

    with make_client() as client:
        objects, over_limit, list_error = _list_all_objects(
            client, bucket, endpoint, ak, sk, prefix, MAX_ZIP_OBJECTS
        )
    if list_error:
        return {"ok": False, "error": list_error}
    if over_limit:
        return {
            "ok": False,
            "error": (
                f"「{prefix}」下的对象数超过打包上限（{MAX_ZIP_OBJECTS} 个），已枚举前 "
                f"{MAX_ZIP_OBJECTS + 1} 个即停止。请换一个更小的前缀，或逐个下载。"
            ),
        }
    if not objects:
        return {"ok": False, "error": f"「{prefix}」下没有可下载的对象"}

    total = sum(size for _, size in objects)
    if total > MAX_ZIP_BYTES:
        return {
            "ok": False,
            "error": (
                f"「{prefix}」下共 {len(objects)} 个对象、{_human_size(total)}，"
                f"超过单次打包上限 {_human_size(MAX_ZIP_BYTES)}。请换一个更小的前缀，或逐个下载。"
            ),
        }

    tmpdir = tempfile.mkdtemp(prefix="vault_oss_zip_")
    part_path = path + ".part"
    failed: list[str] = []
    skipped: list[str] = []
    written = 0
    count = len(objects)
    done = 0
    if progress is not None:
        progress(0, count, f"开始打包 {count} 个对象")

    def _fetch(index: int, key: str) -> tuple[str, str, str]:
        """把一个对象流式拉到本地临时文件，返回 (key, local_path, error)。"""
        if should_cancel is not None and should_cancel():
            raise _Cancelled              # 每个对象开始前查一次：取消后不再有新下载
        local = os.path.join(tmpdir, f"{index}.bin")
        try:
            url = f"https://{bucket}.{endpoint}/{quote(key, safe='/~')}"
            headers = _oss_auth_headers(ak, sk, f"/{bucket}/{key}")
            with make_client() as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        return key, "", f"{key} (HTTP {resp.status_code})"
                    with open(local, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            # 正在下的这个对象也要能随时断流：500 MB 的对象不能等它下完
                            if should_cancel is not None and should_cancel():
                                raise _Cancelled
                            handle.write(chunk)
        except _Cancelled:
            raise          # 取消要往外传，不能被「单个对象失败」当成一个失败项吞掉
        except Exception as exc:  # noqa: BLE001 - 单个对象失败不该毁掉整包
            return key, "", f"{key} ({type(exc).__name__})"
        return key, local, ""

    try:
        with ThreadPoolExecutor(max_workers=_ZIP_WORKERS) as pool:
            futures = [pool.submit(_fetch, i, key) for i, (key, _size) in enumerate(objects)]
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for future in as_completed(futures):
                    key, local, fetch_error = future.result()   # 取消会以 _Cancelled 抛出
                    if should_cancel is not None and should_cancel():
                        raise _Cancelled
                    done += 1
                    if progress is not None:
                        # 上报「已处理对象数 / 对象总数」，text 带当前对象名
                        # （失败与跳过的对象同样计入——它们也是「已经处理完」的）
                        progress(done, count, f"{done}/{count} {key}")
                    if fetch_error:
                        failed.append(fetch_error)
                        continue
                    try:
                        entry = _zip_entry_name(key, prefix)
                        if entry is None:
                            skipped.append(f"{key}（路径不安全）")
                            continue
                        archive.write(local, arcname=entry)
                        written += 1
                    except Exception as exc:  # noqa: BLE001
                        failed.append(f"{key} ({type(exc).__name__})")
                    finally:
                        try:
                            os.remove(local)
                        except OSError:
                            pass
        if written == 0:
            raise RuntimeError("没有对象写入 zip")
        os.replace(part_path, path)      # 同目录内的替换是原子的
    except _Cancelled:                    # 取消：清干净半成品，目标路径始终没被写过
        _remove_quietly(part_path)
        return {"ok": False, "error": "打包已取消"}
    except Exception as exc:  # noqa: BLE001 - 打包失败不留半成品
        _remove_quietly(part_path)
        detail = f"，失败 {len(failed)} 个" if failed else ""
        return {"ok": False, "error": f"打包失败（{type(exc).__name__}{detail}）"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    lines = [f"已打包 {written} 个对象（{_human_size(total)}）到 {path}", f"字节数（未压缩）：{total}"]
    if failed:
        lines.append(f"失败 {len(failed)} 个：" + "；".join(failed[:5]) + ("…" if len(failed) > 5 else ""))
    if skipped:
        lines.append(f"跳过 {len(skipped)} 个：" + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
    lines.append(f"zip 内为相对「{prefix}」的路径")
    return {"ok": True, "output": "\n".join(lines)}


def get_display_summary(data):
    """凭据列表中的简短展示文本（不回显任何敏感字段）。"""
    return "阿里云 | AccessKey (AK/SK)"
