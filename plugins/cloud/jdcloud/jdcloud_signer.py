# coding=utf-8
"""
京东云 (JD Cloud) 只读凭据探针 —— 签名实现经官方 SDK 源码校准。

签名方案: JDCLOUD3-HMAC-SHA256（SDK 内部方法名 signV3），结构与 AWS SigV4 同形。

校准来源（离线解包，非记忆、非二手博客）:
    pip download jdcloud-sdk        # PyPI 版本 1.6.346（当前最新）
    解包后 jdcloud_sdk/core/signer.py / const.py / jdcloudresponse.py

  证据行 1 —— kDate 派生密钥前缀（signer.py 第 171 行，逐字摘录）:
      k_date = self.__sign((const.JDCLOUD3 + key).encode('utf-8'), date_stamp)
  证据行 2 —— 常量定义（const.py 第 17-22 行，逐字摘录）:
      JDCLOUD2 = 'JDCLOUD2'
      JDCLOUD3 = 'JDCLOUD3'
      JDCLOUD_ALGORITHM = 'JDCLOUD2-HMAC-SHA256'
      JDCLOUD3_ALGORITHM = 'JDCLOUD3-HMAC-SHA256'
      JDCLOUD_REQUEST = 'jdcloud2_request'
      JDCLOUD3_REQUEST = 'jdcloud3_request'
  证据行 3 —— 四段派生链（signer.py 第 170-175 行，逐字摘录）:
      def __get_signature_key(self, key, date_stamp, region_name, service_name):
          k_date = self.__sign((const.JDCLOUD3 + key).encode('utf-8'), date_stamp)
          k_region = self.__sign(k_date, region_name)
          k_service = self.__sign(k_region, service_name)
          k_signing = self.__sign(k_service, const.JDCLOUD3_REQUEST)
          return k_signing
  证据行 4 —— 待签串与凭证作用域（signer.py 第 70-92 行，逐字摘录）:
      algorithm = const.JDCLOUD3_ALGORITHM
      credential_scope = (date_str + '/' + region + '/' + service + '/' + const.JDCLOUD3_REQUEST)
      string_to_sign = (algorithm + '\\n' + jdcloud_date + '\\n' + credential_scope + '\\n'
                        + self.__sha256_hash(canonical_request))
      ...
      authorization_header = (
              algorithm + ' ' +
              'Credential=' + credential.access_key + '/' + credential_scope + ', ' +
              'SignedHeaders=' + signed_headers + ', ' +
              'Signature=' + signature)
  证据行 5 —— 错误体解析（jdcloudresponse.py 第 33-45 行，逐字摘录）:
      request_id = resp_dict.get('requestId')
      error = resp_dict.get('error')
      if request_id and error:
          self.error = ErrorResponse(error.get('status'), error.get('code'), error.get('message'))
      result = resp_dict.get('result')

关于「kDate 前缀到底是 JDCLOUD2 还是 JDCLOUD3」的结论:
  * 现行方案（本次采用的）是 **JDCLOUD3**，即 kDate 的 HMAC key = "JDCLOUD3" + secret_key。
    依据: PyPI 1.6.346 源码、GitHub master、readthedocs /en/latest/ 三处一致。
  * "JDCLOUD2" 是**已被废弃的旧版签名方案**（算法名 JDCLOUD2-HMAC-SHA256，
    作用域后缀 jdcloud2_request）。readthedocs 的 `/en/pydoc2/` 页面是**陈旧文档**，
    仍停留在旧方案，会误导（该页确为 `const.JDCLOUD2 + key`）。
  * 全新下载的 1.6.346 中函数名仍叫 signV3、常量叫 JDCLOUD3，二者指同一套现行算法。

自测（模块底部 __main__）直接复放 SDK 自带单元测试 signer_test.py 中的
「固定 AK/SK/时间戳 → 期望 Authorization」向量，离线即可跑，无需网络。

本文件在探针版基础上做了三处**增量**扩展（签名核心 / interpret / 自测向量未改动）：

1. ``_PROBES`` 由「两个 IAM 探针」扩成一张**多服务只读接口表**，每条补 ``service`` /
   ``endpoint`` / ``revision`` / ``region_path`` 四个键。规格逐条取自对应服务的
   ``services/<service>/apis/*Request.py`` 里的 ``super().__init__(path, method, header, version)``
   与 ``services/<service>/client/*Client.py`` 里的 ``Config(endpoint)`` / ``service_name`` / revision。
2. ``sign_probe`` 增加 ``query`` 参数并由接口表驱动；**签名 scope 的 region** 按
   ``region_path`` 决定：路径含 ``{regionId}`` 的用调用方给的 region，不含的用
   ``GLOBAL_SCOPE_REGION``（见该常量注释）。
3. ``build_probe_url`` 改为从接口表读 endpoint，保持与 SDK ``WithoutBodyBuilder`` 一致的
   拼装顺序 ``scheme://endpoint/version/path?query``。

``probe_live`` 仍是**自建 httpx client** 的调试用辅助（保留但不导出给插件调用）：插件侧的
网络请求一律走 ``backend.plugin_sdk.make_client()``，只有那样才会带上该条凭据配置的代理。

签名核心只依赖 Python 标准库（hashlib / hmac / urllib）；仅 ``probe_live`` 这个联网调试
辅助会用到 httpx，且它的 import 是可选的——没装 httpx 时签名与离线自测照常可用。

本模块自带 ``__main__`` 自测：复放官方 SDK 单元测试 signer_test.py 的固定向量，再用
内建样例覆盖 interpret() 的四种判定，离线运行、不联网。

参考文档:
  https://github.com/jdcloud-api/jdcloud-sdk-python           (官方 SDK 仓库)
  https://docs.jdcloud.com/cn/iam/accesskey-management        (AccessKey 管理)
  https://docs.jdcloud.com/cn/iam/api/describeuseraccesskeys  (IAM 接口)
  https://docs.jdcloud.com/cn/cloud-physical-server/api/describeregiones (CPS 地域)
  https://docs.jdcloud.com/cn/virtual-machines/api/describeinstances     (云主机)
  https://docs.jdcloud.com/cn/cloud-disk-service/api/describedisks       (云硬盘)
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote_plus

try:  # httpx 仅 live 探测时需要；离线构造/自测不依赖它
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


# ---------------------------------------------------------------------------
# 常量（逐字对齐 jdcloud_sdk/core/const.py）
# ---------------------------------------------------------------------------
ALGORITHM = "JDCLOUD3-HMAC-SHA256"        # const.JDCLOUD3_ALGORITHM
_SIGN_PREFIX = "JDCLOUD3"                 # const.JDCLOUD3
_SCOPE_SUFFIX = "jdcloud3_request"        # const.JDCLOUD3_REQUEST

# const.ignored_headers = ['authorization', 'user-agent']
_IGNORED_HEADERS = ("authorization", "user-agent")

HDR_AUTH = "Authorization"
HDR_DATE = "x-jdcloud-date"
HDR_NONCE = "x-jdcloud-nonce"
HDR_CONTENT_SHA256 = "x-jdcloud-content-sha256"
HDR_SECURITY_TOKEN = "x-jdcloud-security-token"

_DATE_FMT = "%Y%m%dT%H%M%SZ"


# ---------------------------------------------------------------------------
# 签名核心：逐行移植 jdcloud_sdk/core/signer.py
# ---------------------------------------------------------------------------
def _now_utc_stamp() -> str:
    # SDK: datetime.datetime.utcfromtimestamp(time.time()).strftime('%Y%m%dT%H%M%SZ')
    return datetime.datetime.now(datetime.timezone.utc).strftime(_DATE_FMT)


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    # SDK: __sign
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(val: str) -> str:
    # SDK: __sha256_hash
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def derive_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """四段 HMAC 派生链。

    证据行: k_date = self.__sign((const.JDCLOUD3 + key).encode('utf-8'), date_stamp)
    即 kDate 的 HMAC key 是字符串 "JDCLOUD3" + secret_key。
    """
    k_date = _hmac_sha256((_SIGN_PREFIX + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, _SCOPE_SUFFIX)


def _strip_scheme(full_host: str) -> str:
    # SDK: __build_canonical_host
    if full_host.lower().find("http://") == 0:
        return full_host[7:]
    if full_host.lower().find("https://") == 0:
        return full_host[8:]
    return full_host


def _canonical_uri(path: str) -> str:
    """SDK: __build_canonical_uri —— 先 unquote_plus 解码（'+' 视为空格），
    折叠重复斜杠，再按 safe='/~' 重新百分号编码。"""
    decoded_path = re.sub(r"/+", "/", unquote_plus(path))
    if decoded_path == "":
        return "/"
    encoded_path = quote(decoded_path, "/~")
    if encoded_path.startswith("/"):
        return encoded_path
    return "/" + encoded_path


def _normalize_query_string(query) -> str:
    """SDK: __normalize_query_string —— 对每对 k=v 做 unquote_plus 解码后按 safe='~'
    重新编码，按键字典序排序，丢弃空 key。"""
    params: List[List[str]] = []
    if isinstance(query, str):
        for s in query.split("&"):
            if len(s) <= 0:
                continue
            item = []
            for val in s.split("=", 1):
                item.append(quote(unquote_plus(val), "~"))
            params.append(item)
    elif isinstance(query, dict):
        for key in query.keys():
            params.append([quote(unquote_plus(key), "~"), quote(unquote_plus(query[key]), "~")])

    normalized = ""
    for p in sorted(params):
        if p[0] == "":
            continue
        elif len(p) == 2:
            normalized += "%s=%s&" % (p[0], p[1])
        elif len(p) > 2:
            normalized += "%3D".join(p[1:]) + "&"
    if normalized.endswith("&"):
        normalized = normalized[: len(normalized) - 1]
    return normalized


def _build_canonical_headers(req_headers: Dict[str, str], canonical_host: str) -> Tuple[str, str]:
    """SDK: __build_canonical_headers —— host 固定排首位，其余 header 小写化后整体排序。
    注意：SDK 是先构造规范头、之后才把 x-jdcloud-date / x-jdcloud-nonce 塞进 headers。
    因此**真实客户端**发起的请求，SignedHeaders 通常只有 `content-type;host`。"""
    headers = ["host"]
    signed_values: Dict[str, str] = {}
    for key in req_headers.keys():
        value = req_headers[key]
        lower_key = key.lower()
        if lower_key in _IGNORED_HEADERS:
            continue
        headers.append(lower_key)
        signed_values[lower_key] = value

    headers.sort()
    signed_headers = ";".join(headers)

    canonical_values = []
    for key in headers:
        if key == "host":
            canonical_values.append("host:" + canonical_host.strip())
        else:
            header_value = signed_values[key]
            if isinstance(header_value, str):
                canonical_values.append(key + ":" + header_value.strip())
            elif isinstance(header_value, list):
                canonical_values.append(key + ":" + ",".join(v.strip() for v in header_value))

    return "\n".join(canonical_values) + "\n", signed_headers


def _sign_v3(
    access_key: str,
    secret_key: str,
    method: str,
    service: str,
    region: str,
    host: str,
    path: str,
    query,
    headers: Dict[str, str],
    data: str = "",
    security_token: str = "",
) -> Dict[str, str]:
    """移植版 signV3：就地改写 headers（同 SDK 行为），返回规范化中间结果供调试。

    host 可含 scheme（SDK 测试即如此传参），内部会剥离。
    """
    canonical_host = _strip_scheme(host)
    jdcloud_date = _now_utc_stamp()
    nonce = str(uuid.uuid4())
    if HDR_DATE in headers:
        jdcloud_date = headers[HDR_DATE]
    if HDR_NONCE in headers:
        nonce = headers[HDR_NONCE]
    date_str = jdcloud_date[:8]

    canonical_querystring = _normalize_query_string(query)
    canonical_headers, signed_headers = _build_canonical_headers(headers, canonical_host)
    payload_hash = _sha256_hex(data)

    canonical_request = "\n".join(
        [
            method,
            _canonical_uri(path),
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = date_str + "/" + region + "/" + service + "/" + _SCOPE_SUFFIX
    string_to_sign = "\n".join(
        [ALGORITHM, jdcloud_date, credential_scope, _sha256_hex(canonical_request)]
    )

    signing_key = derive_signing_key(secret_key, date_str, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization_header = (
        ALGORITHM
        + " "
        + "Credential=" + access_key + "/" + credential_scope + ", "
        + "SignedHeaders=" + signed_headers + ", "
        + "Signature=" + signature
    )

    headers.update(
        {
            HDR_AUTH: authorization_header,
            HDR_DATE: jdcloud_date,
            HDR_CONTENT_SHA256: payload_hash,
            # SDK 逐字如此：以常量值同时作为 header 名与值（const.JDCLOUD3_ALGORITHM）
            ALGORITHM: ALGORITHM,
            HDR_NONCE: nonce,
        }
    )
    if security_token:
        headers.update({HDR_SECURITY_TOKEN: security_token})

    return {
        "signature": signature,
        "authorization": authorization_header,
        "canonical_request": canonical_request,
        "string_to_sign": string_to_sign,
        "credential_scope": credential_scope,
        "signed_headers": signed_headers,
        "payload_hash": payload_hash,
    }


# ---------------------------------------------------------------------------
# 只读接口表
# ---------------------------------------------------------------------------
# 每条规格的取证位置（逐字摘录，非记忆、非二手）：
#   services/<service>/apis/<Xxx>Request.py:
#       super(<Xxx>Request, self).__init__('<path>', '<method>', header, version)
#   services/<service>/client/<Xxx>Client.py:
#       config = Config('<endpoint>')
#       super(<Xxx>Client, self).__init__(credential, config, '<service>', '<revision>', logger)
#
# region_path 标记「路径里有没有 {regionId}」，它决定**签名 scope 用哪个 region**：
# core/jdcloudclient.py:102-110 的 __get_region_id 在参数里取不到 regionId 时固定返回
# 'jdcloud-api'，该值一路传进 signer.sign(region=...)。所以路径不含 {regionId} 的接口，
# 签名 region 必须写 GLOBAL_SCOPE_REGION，而不是调用方给的地域代码。
GLOBAL_SCOPE_REGION = "jdcloud-api"       # __get_region_id 的兜底返回值

IAM_ENDPOINT = "iam.jdcloud-api.com"      # IamClient: Config('iam.jdcloud-api.com')
IAM_SERVICE = "iam"                       # IamClient: service_name='iam'
IAM_REVISION = "0.3.24"                   # IamClient: revision='0.3.24'
_SDK_VERSION = "1.2.9"                    # core/version.py: VERSION

_PROBES: Dict[str, Dict[str, object]] = {
    # iam/apis/DescribeUserAccessKeysRequest.py: '/regions/{regionId}/userAccessKeys', 'GET', version='v1'
    # 官方文档: https://docs.jdcloud.com/cn/iam/api/describeuseraccesskeys
    "DescribeUserAccessKeys": {
        "service": IAM_SERVICE,
        "endpoint": IAM_ENDPOINT,
        "revision": IAM_REVISION,
        "method": "GET",
        "path": "/regions/{regionId}/userAccessKeys",
        "version": "v1",
        "query": "",
        "region_path": True,
        "note": "查询主账号 AccessKey 列表（响应含 accessKeySecret，只读且不落库）；"
                "需 IAM 读权限，无权限则返回 AccessDenied(→restricted)",
    },
    # iam/apis/DescribeUserPinRequest.py: '/regions/{regionId}/user:describeUserPin', 'GET', version='v1'
    # 备选自指探针；官方文档站未收录本接口（规格取自 SDK 源码）
    "DescribeUserPin": {
        "service": IAM_SERVICE,
        "endpoint": IAM_ENDPOINT,
        "revision": IAM_REVISION,
        "method": "GET",
        "path": "/regions/{regionId}/user:describeUserPin",
        "version": "v1",
        "query": "",
        "region_path": True,
        "note": "根据 AccessKey/AccountId 获取用户 Pin；自指接口，越权概率低，易拿 200(→valid)",
    },
    # cps/apis/DescribeRegionesRequest.py: '/regions', 'GET', version='v1'
    # CpsClient: Config('cps.jdcloud-api.com'), service_name='cps', revision='2.4.3'
    # 官方文档: https://docs.jdcloud.com/cn/cloud-physical-server/api/describeregiones
    "DescribeRegiones": {
        "service": "cps",
        "endpoint": "cps.jdcloud-api.com",
        "revision": "2.4.3",
        "method": "GET",
        "path": "/regions",
        "version": "v1",
        "query": "",
        "region_path": False,             # 路径无 {regionId} ⇒ 签名 region 用 GLOBAL_SCOPE_REGION
        "note": "查询云物理服务器地域列表（响应含各地域下的可用区）；路径无 regionId 故签名 scope 为 jdcloud-api",
    },
    # vm/apis/DescribeInstancesRequest.py: '/regions/{regionId}/instances', 'GET', version='v1'
    # VmClient: Config('vm.jdcloud-api.com'), service_name='vm', revision='1.6.1'
    # 官方文档: https://docs.jdcloud.com/cn/virtual-machines/api/describeinstances
    "DescribeInstances": {
        "service": "vm",
        "endpoint": "vm.jdcloud-api.com",
        "revision": "1.6.1",
        "method": "GET",
        "path": "/regions/{regionId}/instances",
        "version": "v1",
        "query": "",
        "region_path": True,
        "note": "查询云主机实例列表；pageSize 默认 20、取值 [10,100]",
    },
    # disk/apis/DescribeDisksRequest.py: '/regions/{regionId}/disks', 'GET', version='v1'
    # DiskClient: Config('disk.jdcloud-api.com'), service_name='disk', revision='0.12.8'
    # 官方文档: https://docs.jdcloud.com/cn/cloud-disk-service/api/describedisks
    "DescribeDisks": {
        "service": "disk",
        "endpoint": "disk.jdcloud-api.com",
        "revision": "0.12.8",
        "method": "GET",
        "path": "/regions/{regionId}/disks",
        "version": "v1",
        "query": "",
        "region_path": True,
        "note": "查询云硬盘列表；pageSize 默认 20、取值 [10,100]",
    },
}

DEFAULT_ACTION = "DescribeUserAccessKeys"


def build_probe_url(
    region: str,
    action: str,
    endpoint: Optional[str] = None,
    scheme: str = "https",
) -> str:
    """按 ParameterBuilder.WithoutBodyBuilder 的拼装规则生成 URL：
    '%s://%s/%s%s%s' % (scheme, endpoint, version, path, query)

    ``endpoint`` 为空时取接口表里该 action 自己的端点（端点是按服务区分的，不再全局唯一）。
    """
    spec = _PROBES[action]
    ep = endpoint or str(spec["endpoint"])
    path = str(spec["path"]).replace("{regionId}", region)
    query = str(spec.get("query") or "")
    query_part = ("?" + query) if query else ""
    return "%s://%s/%s%s%s" % (scheme, ep, spec["version"], path, query_part)


def sign_probe(
    ak: str,
    sk: str,
    *,
    region: str = "cn-north-1",
    service: Optional[str] = None,
    action: str = DEFAULT_ACTION,
    endpoint: Optional[str] = None,
    query: Optional[str] = None,
    security_token: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, Dict[str, str], str]:
    """构造一个**只读**请求（不发网络），返回 ``(method, url, headers, body)``。

    ``service`` / ``endpoint`` 为空时取接口表里该 action 自己的值；``query`` 为空时取表里的
    默认 query（本项目所有接口都靠 query 传可选参数，例如 ``pageNumber=1&pageSize=100``）。

    **scope region 的取法**（与 SDK 一致）：路径含 ``{regionId}`` 的接口用调用方给的
    ``region``；路径不含的（如 cps 的 ``/regions``）用 ``GLOBAL_SCOPE_REGION``——
    SDK 的 ``__get_region_id`` 在这种情况下就是返回 ``'jdcloud-api'``。写错这一个字段
    不会报「参数错误」，只会得到 SignatureDoesNotMatch。

    body 对 GET 恒为 ""（WithoutBodyBuilder.build_body -> ''）。headers 内含全部签名头。
    """
    if action not in _PROBES:
        raise ValueError("未知接口 action=%r；可选: %s" % (action, ", ".join(sorted(_PROBES))))
    spec = _PROBES[action]

    svc = service or str(spec["service"])
    ep = endpoint or str(spec["endpoint"])
    q = str(spec.get("query") or "") if query is None else query
    scope_region = region if spec.get("region_path") else GLOBAL_SCOPE_REGION

    url = build_probe_url(region, action, endpoint=ep, scheme="https")
    if q:
        url = url + "?" + q
    method = str(spec["method"])
    body = ""

    # 与 JDCloudClient.__merge_headers 一致：默认带 Content-Type: application/json。
    # User-Agent 会被签名器忽略（const.ignored_headers），保留官方 SDK 的形态只为混流。
    version_tag = "%s/%s" % (svc, spec["revision"]) if spec.get("revision") else svc
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "JdcloudSdkPython/%s %s" % (_SDK_VERSION, version_tag),
    }
    if extra_headers:
        headers.update(extra_headers)

    # 解析出 host / path 交给签名器（同 Signer.sign 的 url->dict 流程）
    host, path = _split_host_path(url)

    _sign_v3(
        access_key=ak,
        secret_key=sk,
        method=method,
        service=svc,
        region=scope_region,
        host=host,
        path=path,
        query=q,
        headers=headers,
        data=body,
        security_token=security_token,
    )
    return method, url, headers, body


def _split_host_path(url: str) -> Tuple[str, str]:
    m = re.match(r"^(?P<scheme>https?)://(?P<host>[^/]+)(?P<path>/.*)?$", url)
    if not m:
        raise ValueError("无法解析 URL: %r" % url)
    host = m.group("host")
    path = m.group("path") or "/"
    if "?" in path:
        path = path.split("?", 1)[0]
    return host, path


def probe_live(
    ak: str,
    sk: str,
    *,
    region: str = "cn-north-1",
    action: str = DEFAULT_ACTION,
    timeout: float = 10.0,
    proxy: Optional[str] = None,
) -> Tuple[int, str]:
    """真实发起只读探针，返回 (status_code, body_text)。需 httpx。"""
    if httpx is None:
        raise RuntimeError("未安装 httpx，无法发起网络探测")
    method, url, headers, body = sign_probe(ak, sk, region=region, action=action)
    with httpx.Client(timeout=timeout, proxy=proxy, follow_redirects=False) as c:
        r = c.request(method, url, headers=headers, content=body)
    return r.status_code, r.text


# ---------------------------------------------------------------------------
# 响应判定
# ---------------------------------------------------------------------------
# 凭据类错误（→ invalid）: AK/SK 本身不存在、被禁用，或签名验算失败。
_INVALID_TOKENS = (
    "invalidaccesskey",
    "invalid access key",
    "invalidaccesskeyid",
    "invalidsecretkey",
    "invalid secret",
    "accesskeynotfound",
    # AK 不存在时网关的原话（实测：4 个服务一致返回
    # HTTP 403 + status=HTTP_FORBIDDEN + message="secret key is null [gw]"）。
    # 网关按 Credential 里的 AK 反查 SK，查不到就是 null ⇒ 该 AK 在京东云不存在。
    # 必须排在 _RESTRICTED_TOKENS 之前命中：否则 status 里的 "forbidden" 会把它误判成
    # 「有效但权限不足」，让一个打错的 / 已删除的 AK 显示成「凭据有效」。
    "secret key is null",
    "access_error",            # AK 为空时网关的原话（401 ACCESS_ERROR）
    "signaturedoesnotmatch",
    "signature does not match",
    "signature mismatch",
    "签名不匹配",
    "authfailure",
)
# 权限类错误（→ restricted）: 凭据有效但当前身份无该接口权限。
_RESTRICTED_TOKENS = (
    "accessdenied",
    "access denied",
    "forbidden",
    "unauthorized",
    "not authorized",
    "no permission",
    "permission denied",
    "权限不足",
    "无权限",
)


def _extract_error(body_text: str) -> Dict[str, str]:
    """按 jdcloudresponse.py 的结构解析错误体:
        {"requestId": "...", "error": {"status": <int|str>, "code": "...", "message": "..."}}
    兼容 error 直接平铺或缺失的情况。"""
    info: Dict[str, str] = {}
    try:
        doc = json.loads(body_text)
    except Exception:
        return info
    if not isinstance(doc, dict):
        return info
    info["requestId"] = str(doc.get("requestId", ""))
    err = doc.get("error")
    if isinstance(err, dict):
        info["code"] = str(err.get("code", ""))
        info["message"] = str(err.get("message", ""))
        info["status"] = str(err.get("status", ""))
    elif isinstance(err, str):
        info["code"] = err
    # 某些接口把错误放在顶层 code/message
    if "code" not in info and doc.get("code"):
        info["code"] = str(doc["code"])
        info["message"] = str(doc.get("message", ""))
    return info


def interpret(status_code: int, body_text: str) -> Dict[str, str]:
    """判定探针结果。

    规则（按优先级）:
      1) body 解析出 error 对象 → 走错误分类（无论 HTTP 状态；官方 SDK 也是靠 body 判错，
         README 示例 `if resp.error is not None: print(resp.error.code, ...)`）。
      2) 命中凭据类 token → invalid
      3) 命中权限类 token → restricted
      4) HTTP 200 → valid
      5) HTTP 403 且无可识别 token → restricted（Forbidden 的状态级兜底）
      6) 其它 → error

    返回 {"verdict": "valid"|"invalid"|"restricted"|"error", "message": str}
    """
    body_text = body_text or ""
    err = _extract_error(body_text)
    haystack = " ".join(
        [err.get("code", ""), err.get("message", ""), err.get("status", "")]
    ).lower()
    raw = body_text.lower()

    def _hit(tokens) -> Optional[str]:
        for t in tokens:
            if t in haystack or t in raw:
                return t
        return None

    has_error_obj = bool(err.get("code") or err.get("message"))

    # 1) 错误对象优先分类
    if has_error_obj or status_code >= 400:
        code = err.get("code", "")
        msg = err.get("message", "") or body_text.strip()[:300]

        tok = _hit(_INVALID_TOKENS)
        if tok:
            return {
                "verdict": "invalid",
                "message": "凭据无效（命中 %r）: code=%s message=%s" % (tok, code, msg),
            }

        tok = _hit(_RESTRICTED_TOKENS)
        if tok:
            return {
                "verdict": "restricted",
                "message": "凭据有效但权限不足（命中 %r）: code=%s message=%s" % (tok, code, msg),
            }

        # 状态级兜底
        if status_code == 403:
            return {
                "verdict": "restricted",
                "message": "HTTP 403 Forbidden（未识别错误码，按权限不足处理）: code=%s message=%s"
                % (code, msg),
            }
        if status_code == 401 and not has_error_obj:
            return {
                "verdict": "error",
                "message": "HTTP 401 但响应体无 JD Cloud 错误结构: %s" % body_text.strip()[:300],
            }
        return {
            "verdict": "error",
            "message": "未识别的错误: http=%s code=%s message=%s" % (status_code, code, msg),
        }

    # 4) 无 error 对象且状态非 4xx/5xx
    if status_code == 200:
        return {"verdict": "valid", "message": "HTTP 200，凭据有效"}
    return {
        "verdict": "error",
        "message": "非预期状态码 %s: %s" % (status_code, body_text.strip()[:300]),
    }


# ---------------------------------------------------------------------------
# 自测：复放官方 SDK 单元测试 signer_test.py 的固定向量（离线，无需网络）
# ---------------------------------------------------------------------------
_SDK_VECTORS = [
    # (name, path, query, expected_signature)  --- 取自 jdcloud_sdk/core/signer_test.py
    (
        "testSign_path_null",
        "",
        "",
        "44c5ec30b6185516986ae171d8276552bc0f37f6a536a44e4f578958fc1c12be",
    ),
    (
        "testSign_path_specialCharacters",
        "/v1/regions/cn-north-1/instances/ /`!@#$%^&*()=+/0123456789/[]\\;',<>?:\"{}|"
        "/abcdefghijklmnopqrstuvwxyz/ABCDEFGHIJKLMNOPQRSTUVWXYZ/-_.~:GET",
        "",
        "925b9455228ef23ee7ec2dd3e33f7e645bcbcb3f4732894c17e4a0cf1d41d1e6",
    ),
    (
        "testSign_path_chineseCharacters",
        "/v1/regions/cn-north-1/instances/中文:GET",
        "",
        "936ed2dcb6fb4271ad640e0f46f1fe61763a0bad8bb4f4eb1224261f969612fa",
    ),
    (
        "testSign_path_doubleSlashes",
        "///v1/regions/cn-north-1/instances//// //`!@#$%^&*()=+/0123456789/[]\\;',<>?:\"{}|"
        "/////abcdefghijklmnopqrstuvwxyz//ABCDEFGHIJKLMNOPQRSTUVWXYZ/-_.~:GET/",
        "",
        "92e4897d2a74a899399f5443615ddf110978363b9097932e522e079e6eb5f65b",
    ),
    (
        "testSign_query_chinese",
        "/v1/regions/cn-north-1/instances",
        "中文参数=中文参数值",
        "7e3094d23adde49fcc99b7d0de911d7ecaa7767590edd57769d7bdbaca919276",
    ),
    (
        "testSign_query_nullKey",
        "/v1/regions/cn-north-1/instances",
        "=a&a=a",
        "43efb5fb00e86faf35d8bf0bed3b72e84cfc98ee4754065e29c3caa7bcaa12cb",
    ),
    (
        "testSign_query_partEncode",
        "/v1/regions/cn-north-1/instances",
        " =blank&%20=blank&+= 2&%2b= 1&blank= &blank=+&blank=%20",
        "78c0a4db84050d773533c78c1a0f73bf974faf5df881e39b18c3e0de8110324e",
    ),
]

# SDK 测试的固定上下文（signer_test.py setUp）
_VEC_AK = "ak"
_VEC_SK = "sk"
_VEC_SERVICE = "apigatewaytestproductline"
_VEC_REGION = "cn-north-1"
_VEC_METHOD = "GET"
_VEC_HOST = "http://apigw-internal-dev.cn-north-1.jcloudcs.com:8000"
_VEC_HEADERS = {
    "x-jdcloud-date": "20190917T064708Z",
    "x-jdcloud-nonce": "X-Jdcloud-Nonce",
    "content-type": "application/json",
}


def _selftest() -> int:
    failures = 0

    print("=" * 78)
    print("[1] 复放官方 SDK 向量（固定 AK/SK/时间戳 → 期望 Signature）")
    print("=" * 78)
    for name, path, query, expected in _SDK_VECTORS:
        hdr = dict(_VEC_HEADERS)
        out = _sign_v3(
            access_key=_VEC_AK,
            secret_key=_VEC_SK,
            method=_VEC_METHOD,
            service=_VEC_SERVICE,
            region=_VEC_REGION,
            host=_VEC_HOST,
            path=path,
            query=query,
            headers=hdr,
            data="",
            security_token="",
        )
        got = out["signature"]
        ok = got == expected
        failures += 0 if ok else 1
        print("  %s %-34s %s" % ("OK  " if ok else "FAIL", name, got))
        if not ok:
            print("       expected: %s" % expected)
    print("  -> %d/%d 向量通过" % (len(_SDK_VECTORS) - failures, len(_SDK_VECTORS)))

    print()
    print("=" * 78)
    print("[2] 探针构造（默认 DescribeUserAccessKeys）")
    print("=" * 78)
    method, url, headers, body = sign_probe("AKIDEXAMPLE", "SKEXAMPLE")
    print("  method :", method)
    print("  url    :", url)
    print("  body   :", repr(body))
    for k, v in headers.items():
        print("  header : %s: %s" % (k, v))
    assert method == "GET", method
    assert url == "https://iam.jdcloud-api.com/v1/regions/cn-north-1/userAccessKeys", url
    assert body == "", body
    assert ALGORITHM in headers[HDR_AUTH]
    assert "Credential=AKIDEXAMPLE/" in headers[HDR_AUTH]
    assert "SignedHeaders=content-type;host" in headers[HDR_AUTH], headers[HDR_AUTH]
    assert headers[HDR_CONTENT_SHA256] == hashlib.sha256(b"").hexdigest()
    print("  -> 断言通过（URL / 空 body / SignedHeaders=content-type;host / 空载荷哈希）")

    print()
    print("  备选自指探针 DescribeUserPin:")
    _, url2, hdr2, _ = sign_probe("AKIDEXAMPLE", "SKEXAMPLE", action="DescribeUserPin")
    print("    url :", url2)
    assert url2 == "https://iam.jdcloud-api.com/v1/regions/cn-north-1/user:describeUserPin"
    print("    auth:", hdr2[HDR_AUTH])

    print()
    print("=" * 78)
    print("[3] interpret() 判定")
    print("=" * 78)
    # 200 正常
    body_ok = json.dumps({"requestId": "req-1", "result": {"accessKeys": []}})
    cases = [
        ("200 result", 200, body_ok, "valid"),
        # 凭据无效（签名不匹配 / ACCESS_ERROR / InvalidAccessKey）
        ("401 SignatureDoesNotMatch", 401,
         json.dumps({"requestId": "req-2", "error": {"status": "401",
                    "code": "SignatureDoesNotMatch",
                    "message": "The request signature we calculated does not match"}}), "invalid"),
        ("401 ACCESS_ERROR", 401,
         json.dumps({"requestId": "req-3", "error": {"status": "401",
                    "code": "ACCESS_ERROR", "message": "access key error"}}), "invalid"),
        ("401 InvalidAccessKey", 401,
         json.dumps({"requestId": "req-4", "error": {"status": "401",
                    "code": "InvalidAccessKey", "message": "invalid access key"}}), "invalid"),
        # AK 不存在：网关按 AK 反查不到 SK 时的原话（实测响应体逐字复刻）。
        # 注意 error.status 是 "HTTP_FORBIDDEN"，必须仍判 invalid 而不是被 "forbidden" 带偏。
        ("403 secret key is null", 403,
         json.dumps({"error": {"message": "secret key is null [gw]",
                               "status": "HTTP_FORBIDDEN", "code": 403},
                     "requestId": "req-4b"}), "invalid"),
        # 凭据有效但无权限
        ("403 AccessDenied", 403,
         json.dumps({"requestId": "req-5", "error": {"status": "403",
                    "code": "AccessDenied", "message": "无权限"}}), "restricted"),
        ("403 bare", 403, "", "restricted"),
        # 其它
        ("500 非识别", 500, json.dumps({"requestId": "req-6",
                                        "error": {"status": "500", "code": "InternalError",
                                                  "message": "boom"}}), "error"),
        ("502 HTML", 502, "<html>bad gateway</html>", "error"),
    ]
    for label, st, bt, want in cases:
        got = interpret(st, bt)
        ok = got["verdict"] == want
        failures += 0 if ok else 1
        print("  %s %-26s -> %-10s %s" % ("OK  " if ok else "FAIL", label, got["verdict"], got["message"]))
        if not ok:
            print("       expected verdict=%s" % want)

    print()
    if failures == 0:
        print("SELFTEST: ALL PASSED")
    else:
        print("SELFTEST: %d FAILURE(S)" % failures)
    return failures


if __name__ == "__main__":
    raise SystemExit(_selftest())
