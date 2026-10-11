# -*- coding: utf-8 -*-
"""自包含的百度智能云 BCE(Baidu Cloud Engine)只读探针签名实现。

用途：在不引入官方 SDK 的前提下，构造一个「能被 BCE 服务端接受」的已签名只读请求，
用返回的错误码判定一对 AK/SK 的有效性。

实现严格对齐官方 SDK `baidubce/bce-sdk-python`（校准 commit:
b1979d1e63240b88ef8c68c0456800e0d9ae31bf, SDK_VERSION=0.9.79）中的：

- ``baidubce/auth/bce_v1_signer.py``  —— ``_get_canonical_headers`` / ``sign``
- ``baidubce/utils.py``               —— ``get_canonical_time`` / ``normalize_string`` / ``get_canonical_querystring``
- ``baidubce/http/bce_http_client.py``—— ``send_request`` 里 Host / Content-Length / x-bce-date 的先后次序

只依赖标准库与 httpx(仅可选发送用)。

依赖：stdlib + httpx
参考文档：
- 生成认证字符串：https://cloud.baidu.com/doc/Reference/s/njwvz1yfu
- 签名流程：https://cloud.baidu.com/doc/Reference/s/6k6yfh1he
- IAM API 概述(服务域名 iam.bj.baidubce.com)：https://cloud.baidu.com/doc/IAM/s/Pjx35b92m
- IAM 错误码：https://cloud.baidu.com/doc/IAM/s/Rjx4d0rxo
- 通用错误码(RequestExpired 等)：https://cloud.baidu.com/doc/Reference/s/Bjz3uz3qj
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "sign_probe",
    "interpret",
    "send_probe",
    "PROBES",
    "DEFAULT_SIGNED_HEADERS",
    "normalize_string",
    "canonical_time",
    "canonical_headers",
    "canonical_querystring",
    "sign_authorization",
]

# --------------------------------------------------------------------------- #
# 常量（对齐 bce_v1_signer.py / http_headers.py）
# --------------------------------------------------------------------------- #

#: x-bce- 前缀头域一律参与签名（http_headers.BCE_PREFIX = b"x-bce-"）
BCE_PREFIX = "x-bce-"

#: signedHeaders 留空时服务端采用的「推荐集合」——见 bce_v1_signer._get_canonical_headers
#: 的默认值 set([host, content-md5, content-length, content-type])，
#: 与官方文档「Host、Content-Length、Content-Type、Content-MD5、所有 x-bce- 开头的 Header」一致。
DEFAULT_SIGNED_HEADERS = frozenset({"host", "content-md5", "content-length", "content-type"})

#: 认证串里的签名有效期（秒）。官方示例一律用 1800(30 分钟)。
DEFAULT_EXPIRATION = 1800

#: 服务端允许的 x-bce-date 与服务器时间最大偏差。超过即返回 RequestExpired(400)。
#: 官方文档口径：两者相差超过 30 分钟即判请求过期。
MAX_CLOCK_SKEW_SECONDS = 1800

#: 探针清单：名称 -> (host, region, method, path, params)
#: - IAM   : 官方 test/iam/test_iam_client.py 中 HOST = 'https://iam.bj.baidubce.com:80'
#: - BOS   : 官方默认域名后缀 bcebos.com（baidubce/__init__.py DEFAULT_SERVICE_DOMAIN），
#:           区域拼接形式为 <region>.bcebos.com，北京=bj
PROBES: Dict[str, Dict[str, Any]] = {
    "iam": {
        "host": "iam.bj.baidubce.com",
        "region": "bj",
        "method": "GET",
        "path": "/v1/user",
        "params": {},
        "url": "https://iam.bj.baidubce.com/v1/user",
    },
    "bos": {
        "host": "bj.bcebos.com",
        "region": "bj",
        "method": "GET",
        "path": "/",
        "params": {},
        "url": "https://bj.bcebos.com/",
    },
}

# --------------------------------------------------------------------------- #
# 基础工具（逐行对照 baidubce/utils.py）
# --------------------------------------------------------------------------- #

# utils._get_normalized_char_list(): 未保留字符 = 字母 + 数字 + ".-_~"，其余按 %XX(大写) 编码
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def _to_bytes(value: Any) -> bytes:
    """复刻 compat.convert_to_bytes：str/bytes 统一成 bytes。"""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def normalize_string(value: Any, encode_slash: bool = True) -> str:
    """逐字节百分号编码。

    对齐 ``utils.normalize_string``：仅保留 letters/digits 与 ``.~-_``；
    当 ``encode_slash=False`` 时保留 ``/``。输出为 ASCII，%XX 用大写十六进制。
    """
    out = []
    for b in _to_bytes(value):
        ch = chr(b)
        if ch in _UNRESERVED:
            out.append(ch)
        elif ch == "/" and not encode_slash:
            out.append("/")
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def canonical_time(timestamp: Optional[float] = None) -> str:
    """UTC 规范时间，形如 ``2015-04-27T08:23:49Z``。

    对齐 ``utils.get_canonical_time``：timestamp 为 0/None 时取当前 UTC。
    """
    if not timestamp:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
    )


def canonical_headers(
    headers: Mapping[str, Any],
    headers_to_sign: Optional[Iterable[str]] = None,
) -> str:
    """构造 CanonicalHeaders。

    直接复刻 ``bce_v1_signer._get_canonical_headers``：

    1. 头域名 ``strip().lower()``，值 ``strip()``；
    2. **值为空的头域跳过**（``if not value: continue``）；
    3. 只保留「``x-bce-`` 前缀」或「落在签名集合内」的头域；
    4. 名与值各自百分号编码后拼成 ``name:value``；
    5. 对整串 ``sort()``（按 ``name:value`` 全串排序，不是按名），再用 ``\\n`` 连接。

    注意第 5 步的排序对象是整条 ``name:value``，因此
    ``content-md5:...`` < ``host:...`` < ``x-bce-a:...``。
    """
    if headers_to_sign:
        to_sign = {h.strip().lower() for h in headers_to_sign}
    else:
        to_sign = set(DEFAULT_SIGNED_HEADERS)

    items = []
    for key, value in headers.items():
        name = _to_str(key).strip().lower()
        val = _to_str(value).strip()
        if not val:  # 空值不参与签名
            continue
        if name.startswith(BCE_PREFIX) or name in to_sign:
            items.append("%s:%s" % (normalize_string(name), normalize_string(val)))
    items.sort()
    return "\n".join(items)


def canonical_querystring(params: Optional[Mapping[str, Any]]) -> str:
    """构造 CanonicalQueryString。

    对齐 ``utils.get_canonical_querystring``：``k=v`` 各自百分号编码（``/`` 也编码），
    ``None`` 视作空串，最后整体 ``sort()`` 后以 ``&`` 连接。
    """
    if not params:
        return ""
    out = []
    for key, value in params.items():
        if value is None:
            value = ""
        out.append("%s=%s" % (normalize_string(key), normalize_string(value)))
    out.sort()
    return "&".join(out)


def sign_authorization(
    access_key: str,
    secret_key: str,
    method: str,
    path: str,
    headers: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    *,
    timestamp: Optional[float] = None,
    expiration: int = DEFAULT_EXPIRATION,
    headers_to_sign: Optional[Iterable[str]] = None,
) -> str:
    """生成 Authorization（BCE V1 签名）。

    严格复刻 ``bce_v1_signer.sign``：

    .. code-block:: text

        authStringPrefix = "bce-auth-v1/{ak}/{timestamp}/{expiration}"
        signingKey       = HMAC-SHA256-HEX(sk, authStringPrefix)
        canonicalRequest = method "\\n" path "\\n" canonicalQuery "\\n" canonicalHeaders
        signature        = HMAC-SHA256-HEX(signingKey, canonicalRequest)
        authorization    = authStringPrefix "/" signedHeaders "/" signature

    ``headers_to_sign`` 省略时 signedHeaders 段留空，authorization 呈现
    ``.../{expiration}//{signature}``（中间两个斜杠）——即官方文档所说的
    「使用默认签名方式，signedHeaders 内容留空」。
    """
    ts = canonical_time(timestamp)
    prefix = "bce-auth-v1/%s/%s/%d" % (access_key, ts, expiration)

    signing_key = hmac.new(
        _to_bytes(secret_key), _to_bytes(prefix), hashlib.sha256
    ).hexdigest()

    # 注意：canonical_uri 直接用 path 原文，SDK 未再做编码
    string_to_sign = "\n".join([
        method,
        path,
        canonical_querystring(params),
        canonical_headers(headers, headers_to_sign),
    ])

    signature = hmac.new(
        _to_bytes(signing_key), _to_bytes(string_to_sign), hashlib.sha256
    ).hexdigest()

    signed_headers = ";".join(headers_to_sign) if headers_to_sign else ""
    return "%s/%s/%s" % (prefix, signed_headers, signature)


# --------------------------------------------------------------------------- #
# 探针构造
# --------------------------------------------------------------------------- #

def sign_probe(
    ak: str,
    sk: str,
    *,
    probe: str = "iam",
    timestamp: Optional[float] = None,
    expiration: int = DEFAULT_EXPIRATION,
) -> Tuple[str, str, Dict[str, str], Optional[bytes]]:
    """构造一个已签名的只读探测请求。

    :param ak: Access Key ID
    :param sk: Secret Access Key
    :param probe: ``"iam"``(默认，``GET /v1/user``) 或 ``"bos"``(``GET https://bj.bcebos.com/``)
    :param timestamp: 签名时刻(epoch 秒)。省略=当前时刻。测试时可固定以获得可复现签名。
    :param expiration: 签名有效期(秒)，默认 1800。
    :return: ``(method, url, headers, body)``，body 恒为 ``None``(只读 GET)。

    Header 次序对齐 ``bce_http_client.send_request``：先塞 Host，空 body 时补
    ``Content-Length: 0``，再写入与 ``timestamp`` 一致的 ``x-bce-date``，最后签名。
    因为 ``Content-Length`` 属于推荐签名集合且值为 ``0``(非空)，它会进入
    CanonicalHeaders —— 也就是说**线上请求必须真的带上 ``Content-Length: 0``**，
    否则服务端重算的 CanonicalHeaders 会不一致。``httpx`` 对无 body 的 GET 不会自动补
    Content-Length，需依赖本函数显式给出的该头。
    """
    spec = PROBES.get(probe)
    if spec is None:
        raise ValueError("unknown probe %r (expected one of %s)" % (probe, sorted(PROBES)))

    headers: Dict[str, str] = {
        "Host": spec["host"],
        "Content-Length": "0",
        "x-bce-date": canonical_time(timestamp),
    }

    headers["Authorization"] = sign_authorization(
        ak, sk,
        spec["method"], spec["path"], headers, spec["params"],
        timestamp=timestamp, expiration=expiration,
    )
    return spec["method"], spec["url"], headers, None


# --------------------------------------------------------------------------- #
# 结果判读
# --------------------------------------------------------------------------- #

_CODE_KEYS = ("code", "Code", "errorCode", "ErrorCode", "error_code")
_MSG_KEYS = ("message", "Message", "msg", "Msg", "errorMessage", "ErrorMessage", "error_description")

_XML_TAG_RE = re.compile(r"<\s*([A-Za-z_][\w.:-]*)\s*>(.*?)<\s*/\s*\1\s*>", re.S)


def _lookup_ci(mapping: Mapping[str, Any], keys) -> Optional[str]:
    lower = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lower:
            value = lower[key.lower()]
            if value is not None and str(value).strip() != "":
                return str(value).strip()
    return None


def _extract_json(text: str) -> Optional[Dict[str, str]]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "code": _lookup_ci(data, _CODE_KEYS),
        "message": _lookup_ci(data, _MSG_KEYS),
    }


def _extract_xml(text: str) -> Optional[Dict[str, str]]:
    # 先按 XML 解析；失败再退化为正则（应对带命名空间/前缀的畸形体）
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = None
    if root is not None:
        flat: Dict[str, str] = {}
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if node.text and node.text.strip():
                flat.setdefault(tag, node.text.strip())
        if flat:
            return {
                "code": _lookup_ci(flat, _CODE_KEYS),
                "message": _lookup_ci(flat, _MSG_KEYS),
            }

    flat = {}
    for match in _XML_TAG_RE.finditer(text):
        flat.setdefault(match.group(1), match.group(2).strip())
    if not flat:
        return None
    return {
        "code": _lookup_ci(flat, _CODE_KEYS),
        "message": _lookup_ci(flat, _MSG_KEYS),
    }


def _extract_code_message(body_text: str, content_type: str = "") -> Tuple[Optional[str], Optional[str]]:
    """从响应体里抠出 Code/Message。

    官方文档口径：BOS 返回 **XML**（``<Error><Code>..</Code><Message>..</Message></Error>``），
    IAM 返回 **JSON**（``{"code":..,"message":..,"requestId":..}``）。
    但**实探测得**：``bj.bcebos.com`` 与 ``iam.bj.baidubce.com`` 目前都返回 **JSON**
    （403/404 错误体均为 ``{"code":..,"message":..,"requestId":..}``，Content-Type 为
    ``application/json``）。因此本函数不依赖 Content-Type 硬判，而是两者都试。
    ``content_type`` 仅作优先顺序提示。
    """
    if not body_text:
        return None, None
    text = body_text.strip()
    ct = (content_type or "").lower()

    order = []
    if "json" in ct:
        order = [_extract_json, _extract_xml]
    elif "xml" in ct:
        order = [_extract_xml, _extract_json]
    else:
        order = [_extract_json, _extract_xml]

    for parser in order:
        parsed = parser(text)
        if parsed and (parsed.get("code") or parsed.get("message")):
            return parsed.get("code"), parsed.get("message")
    return None, None


# 归类规则（任务口径）
#: 判定为 invalid 的错误码。
#: 前两项来自任务口径/官方错误码表；``IamSignatureInvalid`` 是**实探测得**的补充：
#: 现代 IAM 对「AK 不存在」并不返回 ``InvalidAccessKeyId``，而是返回
#: 403 ``IamSignatureInvalid`` / "Could not find credential."（见 __main__ 实探用例）。
#: 若不并入，IAM 探针遇到无效 AK 会一律落进 error，失去判定意义。
_INVALID_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "IamSignatureInvalid"}

#: 便于给出更有信息量的 message
_CODE_HINTS = {
    "AccessDenied": "该 AK 存在但无权限访问此资源（凭证有效但受限）",
    "InvalidAccessKeyId": "Access Key ID 不存在",
    "SignatureDoesNotMatch": "签名校验失败（AK 存在但 SK 不匹配，或本地实现有偏差）",
    "IamSignatureInvalid": "IAM 未找到对应凭证（AK 不存在；或临时凭证/SK 不匹配）",
    "RequestExpired": "请求已过期：x-bce-date 与服务器时间偏差过大（旧版错误码）",
    "RequestTimeTooSkewed": "请求时间与服务器时间偏差过大（实探 403，校时后重试）",
    "InvalidHTTPAuthHeader": "Authorization 头格式非法",
    "InvalidHTTPRequest": "HTTP 请求体有误",
    "InvalidURI": "无法解析请求 URI",
    "OptInRequired": "该 AK 未开通对应服务",
    "NoSuchBucket": "签名已通过，指定桶不存在（若为桶级探针，说明凭证有效）",
    "NoSuchKey": "签名已通过，指定对象不存在（若为对象级探针，说明凭证有效）",
    "InternalError": "服务端内部错误",
}


def interpret(status_code: int, body_text: str, content_type: str = "") -> Dict[str, str]:
    """把探测响应映射成 ``{"verdict", "message"}``。

    verdict 取值：

    - ``valid``      : HTTP 200
    - ``invalid``    : ``InvalidAccessKeyId`` / ``SignatureDoesNotMatch``
    - ``restricted`` : ``AccessDenied``（AK 有效但无该资源权限）
    - ``error``      : 其余（含 RequestExpired、网络/网关错误、无法解析的错误体）

    说明：状态码不足以判定，必须解析响应体的 Code。以下均为**实探测得**的现状：

    - IAM 未知 AK         -> 403 ``IamSignatureInvalid``（非 ``InvalidAccessKeyId``）
    - BOS 签名不匹配       -> 403 ``SignatureDoesNotMatch``（官方通用文档写 400，BOS 实际给 403）
    - 时间偏差过大(IAM/BOS)-> 403 ``RequestTimeTooSkewed``（旧版通用文档写 400 ``RequestExpired``）
    - BOS 桶不存在         -> 404 ``NoSuchBucket``

    即：403 与 400 的分界在真实服务上并不严格遵循通用文档；``RequestExpired``(400) 是
    历史错误码，新服务端已改用 ``RequestTimeTooSkewed``(403)。故本函数一律以 Code 为准。
    """
    code, message = _extract_code_message(body_text or "", content_type or "")

    if status_code == 200:
        verdict = "valid"
        detail = "探针请求成功（AK/SK 有效）"
    elif code in _INVALID_CODES:
        verdict = "invalid"
        detail = _CODE_HINTS.get(code, "")
    elif code == "AccessDenied":
        verdict = "restricted"
        detail = _CODE_HINTS["AccessDenied"]
    else:
        verdict = "error"
        detail = _CODE_HINTS.get(code or "", "")

    bits = []
    if code:
        bits.append("Code=%s" % code)
    if message:
        bits.append("Message=%s" % message)
    if detail:
        bits.append(detail)
    if not bits:
        bits.append("HTTP %s，响应体无可识别错误码" % status_code)
    return {"verdict": verdict, "message": "；".join(bits)}


# --------------------------------------------------------------------------- #
# 可选：真正发出去（httpx）
# --------------------------------------------------------------------------- #

def send_probe(
    ak: str,
    sk: str,
    *,
    probe: str = "iam",
    timeout: float = 10.0,
    proxy: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, str]:
    """签名并发送探针，返回 ``interpret`` 的结果（附 ``status``）。

    需要 httpx。``proxy`` 例：``"socks5://127.0.0.1:10808"``（需 httpx[socks]）。
    """
    import httpx  # 延迟导入，保证本模块的签名部分零第三方依赖

    method, url, headers, body = sign_probe(ak, sk, probe=probe, timestamp=timestamp)
    kwargs: Dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    if proxy:
        kwargs["proxy"] = proxy

    with httpx.Client(**kwargs) as client:
        resp = client.request(method, url, headers=headers, content=body)

    result = interpret(resp.status_code, resp.text, resp.headers.get("Content-Type", ""))
    result["status"] = str(resp.status_code)
    return result


# --------------------------------------------------------------------------- #
# 自测：与官方 SDK 逐字节对齐的固定向量
# --------------------------------------------------------------------------- #
# 向量来源：用官方 bce-sdk-python(b1979d1e) 的 bce_v1_signer.sign 实跑得到
# （见 tmp/bce_sdk_src）。AK=my_ak / SK=my_sk 为该 SDK 自带单测 test_get_sign 使用的值。
#
# 另注：SDK 里 test/bos/test_client.py:1634 有一处被注释掉的历史向量
#   bce-auth-v1/bb37e6dfffc948a59eb5ddd254263809/2014-06-13T05:57:36Z/1800//a20b4334...
# 它引用的 SK 来自环境变量 BOS_SK，仓库内不可得；且该文件是 Python2 遗留
# (headers 用 str 键，py3 下 _get_canonical_headers 会 TypeError)，故不可直接复现，
# 仅作为结构佐证(形如 .../1800//<sig>，signedHeaders 段为空)。

_HIST_INPUTS = dict(
    method="PUT",
    path="/bucket/object1",
    headers={"Host": "bce.baidu.com", "abc": "123", "x-bce-meta-key1": "ABC"},
    params={"A": None, "b": "", "C": "d"},
    timestamp=1402639056,
    expiration=1800,
)

# 线上形态(带 Content-Length: 0)，用官方 SDK 实跑得到的期望值
_EXPECT_IAM = ("bce-auth-v1/my_ak/2024-01-01T00:00:00Z/1800//"
               "5b8015072eeff9921bc016c9d8fa0a690ec11997b4a844217c6b1c3efb89aad4")
_EXPECT_BOS = ("bce-auth-v1/my_ak/2024-01-01T00:00:00Z/1800//"
               "0d6c4f9e92136e14702252eb8b98a5dfa8abdbe81b9022990e162a8659a8bdd7")
_EXPECT_HIST = ("bce-auth-v1/my_ak/2014-06-13T05:57:36Z/1800//"
                "94cfd1c51d14e4b3a77ce558b7eeb44b09f58d6bb690ee996778ec24c7f21845")


def _self_test() -> int:
    failures = []

    def check(label: str, got: Any, want: Any) -> None:
        ok = got == want
        print("%-42s %s" % (label, "OK" if ok else "FAIL"))
        if not ok:
            print("    got : %r" % (got,))
            print("    want: %r" % (want,))
            failures.append(label)

    # 1) CanonicalHeaders —— 对应 SDK test_get_canonical_headers 的断言
    #    {"Host":"localhost","x-bce-a":"a/b:c","C":""} + Content-MD5:MD5
    h = {"Host": "localhost", "x-bce-a": "a/b:c", "C": ""}
    check("canonical_headers(无 MD5)",
          canonical_headers(h),
          "host:localhost\nx-bce-a:a%2Fb%3Ac")
    h["Content-MD5"] = "MD5"
    check("canonical_headers(含 MD5)",
          canonical_headers(h),
          "content-md5:MD5\nhost:localhost\nx-bce-a:a%2Fb%3Ac")

    # 2) 历史向量输入(无 Content-Length) —— 与 SDK 实跑一致
    check("sign_authorization(历史输入)",
          sign_authorization("my_ak", "my_sk", **_HIST_INPUTS),
          "bce-auth-v1/my_ak/2014-06-13T05:57:36Z/1800//"
          "f994c1de4fe2c647e8d13e8cf357bcf12cc49e40aeca12fd32a1caa9d011ac26")

    # 3) 线上形态(IAM)：带 Host / Content-Length:0 / x-bce-date
    for name, expect, host in (("iam", _EXPECT_IAM, "iam.bj.baidubce.com"),
                               ("bos", _EXPECT_BOS, "bj.bcebos.com")):
        method, url, headers, body = sign_probe(
            "my_ak", "my_sk", probe=name, timestamp=1704067200)
        check("sign_probe(%s).Authorization" % name, headers["Authorization"], expect)
        check("sign_probe(%s).x-bce-date" % name, headers["x-bce-date"], "2024-01-01T00:00:00Z")
        check("sign_probe(%s).Host" % name, headers["Host"], host)
        check("sign_probe(%s).method/body" % name, (method, body), ("GET", None))

    # 4) 带 Content-Length 的历史输入 → 期望值（证明 CL 参与签名）
    hist_wire = dict(_HIST_INPUTS)
    hist_wire["headers"] = {b"Host": b"bce.baidu.com", b"Content-Length": 0,
                            b"x-bce-date": b"2014-06-13T05:57:36Z"}
    check("sign_authorization(历史+CL:0)",
          sign_authorization("my_ak", "my_sk", **hist_wire), _EXPECT_HIST)

    # 5) interpret 归类
    check("interpret(200)", interpret(200, "")["verdict"], "valid")
    check("interpret(IAM InvalidAccessKeyId/403)",
          interpret(403, '{"code":"InvalidAccessKeyId","message":"The Access Key ID you provided does not exist in our records.","requestId":"x"}')["verdict"],
          "invalid")
    check("interpret(BOS Xml SignatureDoesNotMatch/400)",
          interpret(400, '<?xml version="1.0"?><Error><Code>SignatureDoesNotMatch</Code>'
                         '<Message>The request signature we calculated does not match</Message>'
                         '<RequestId>r</RequestId></Error>', "application/xml")["verdict"],
          "invalid")
    check("interpret(AccessDenied/403)",
          interpret(403, '{"code":"AccessDenied","message":"Access denied."}')["verdict"],
          "restricted")
    check("interpret(RequestExpired/400)",
          interpret(400, '{"code":"RequestExpired","message":"Request has expired. Timestamp date is X."}')["verdict"],
          "error")
    check("interpret(空体 500)",
          interpret(500, "")["verdict"], "error")

    # 6) 实探测得的真实响应体（2026-10，bj 区域，直连）
    check("interpret(实探 IAM IamSignatureInvalid/403)",
          interpret(403, '{"message":"IamSignatureInvalid, cause: Could not find credential.",'
                         '"code":"IamSignatureInvalid","requestId":"5ced308e"}')["verdict"],
          "invalid")
    check("interpret(实探 IAM RequestTimeTooSkewed/403)",
          interpret(403, '{"message":"The difference between the request time and the '
                         'server\'s time is too large.","code":"RequestTimeTooSkewed"}')["verdict"],
          "error")
    check("interpret(实探 BOS SignatureDoesNotMatch/403 JSON)",
          interpret(403, '{"code":"SignatureDoesNotMatch","message":"The request signature we '
                         'calculated does not match","requestId":"b"}', "application/json; charset=utf-8")["verdict"],
          "invalid")
    check("interpret(实探 BOS NoSuchBucket/404)",
          interpret(404, '{"code":"NoSuchBucket","message":"The specified bucket does not exist."}',
                    "application/json; charset=utf-8")["verdict"],
          "error")

    print()
    if failures:
        print("自测失败 %d 项：%s" % (len(failures), failures))
        return 1
    print("全部自测通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
