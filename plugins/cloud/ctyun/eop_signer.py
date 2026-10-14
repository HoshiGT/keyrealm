"""天翼云 (CTyun, ctyun.cn) EOP 只读探针签名实现。

本模块是**经官方 SDK 源码逐字校准**的天翼云 EOP 签名（"eop" = 天翼云统一开放平台网关），
用于构造一个只读探针：``GET /v4/region/list-regions``（资源池列表查询）。
凭据有效时该接口返回本租户可见的资源池列表，不会产生任何写副作用。

自包含：仅依赖标准库。

============================================================================
证据来源（校准基准）
============================================================================
天翼云**没有官方 Python SDK**（PyPI 上的 ``ctyun`` 是无关的库存管理包；GitHub 上
``fatelei/pyctyun`` 是第三方）。官方 GitHub 组织 ``ctyun-it`` 提供的是 **Go SDK**
与 Terraform Provider：

  * 官方 Go SDK： https://github.com/ctyun-it/ctyun-sdk-go
      - 签名实现在 ``ctyun-sdk-core/request.go`` 的 ``GetSign()``
      - 成功判定在 ``ctyun-sdk-core/response.go``（``StatusCodeSuccessString = "800"``）
      - 端点在 ``ctyun-sdk-endpoint/ctecs/const.go`` 与 ``.../ctecs/common/const.go``
      - list-regions 定义在 ``ctyun-sdk-endpoint/ctecs/region_list.go`` 与
        ``.../ctecs/common/region_list_regions.go``
  * 官方文档（中国大陆站）：
      - 资源池列表查询 https://www.ctyun.cn/document/10026730/10040588
      - 认证鉴权 / 构造待签名字符串 / 构造动态密钥（同目录）
  * 国际站（eSurfing，同源文档，可镜像抓取）：
      - https://www.esurfingcloud.com/document/zh-cn/10027004/10028368 （构造待签名字符串）
      - https://www.esurfingcloud.com/document/zh-cn/10027004/10028371 （构造动态密钥）

关键源码片段（官方 Go SDK ``ctyun-sdk-core/request.go``）::

    // GetSign 加签
    func GetSign(query string, body []byte, tim time.Time, uuid string, credential Credential) string {
        hash := sha256.New()
        hash.Write(body)
        sum := hash.Sum(nil)
        calculateContentHash := hex.EncodeToString(sum)
        date := tim.Format("20060102T150405Z")
        sigture := fmt.Sprintf("ctyun-eop-request-id:%s\\neop-date:%s\\n\\n%s\\n%s", uuid, date, query, calculateContentHash)
        singerDd := tim.Format("20060102")
        kAk := hmacSHA256(credential.ak, string(hmacSHA256(date, credential.sk)))
        kdate := hmacSHA256(singerDd, string(kAk))
        signaSha256 := hmacSHA256(sigture, string(kdate))
        Signature := base64.StdEncoding.EncodeToString(signaSha256)
        signHeader := credential.ak + " Headers=ctyun-eop-request-id;eop-date Signature=" + Signature
        return signHeader
    }

    // hmacSHA256 注意入参顺序：第一个参数是"消息"，第二个参数是"密钥"
    func hmacSHA256(signature string, key string) []byte {
        m := hmac.New(sha256.New, []byte(key))
        m.Write([]byte(signature))
        return m.Sum(nil)
    }

以及请求构造 ``buildRequest()`` ::

    tim := time.Now()                          // 本地时间，非 UTC
    eopDate := tim.Format("20060102T150405Z")  // 布局里的 Z 是字面量
    id := uuid.NewString()
    sign := GetSign(u.RawQuery, c.body, tim, id, c.credential)
    headers.Add("ctyun-eop-request-id", id)
    headers.Add("Eop-Authorization", sign)
    headers.Add("Eop-date", eopDate)

以及成功判定 ``ctyun-sdk-core/response.go`` ::

    const StatusCodeSuccessString = "800"
    func (c CtyunResponseModel) IsSuccess() bool {
        return c.ParseStatusCode() == StatusCodeSuccessString  // statusCode == "800"
    }

============================================================================
钉死的结论
============================================================================
1) 待签串（sigture）的确切拼装::

       "ctyun-eop-request-id:{uuid}\neop-date:{eop_date}\n\n{query}\n{body_sha256_hex}"

   - header 段：`header_name:header_value`，按 header 名升序，以 `\n` 结尾。
     本次只签两个强制头：``ctyun-eop-request-id``、``eop-date``（字典序 c<e）。
   - header 段与 query 段之间是 **两个 `\\n`**（header 段尾部一个 + 分隔一个，
     即一个空行）。官方文档示例的 header 段本身就带尾随 `\n`，再加分隔符即为 2 个。
   - query 段与 body 哈希段之间是 **一个 `\\n`**。
   - body 哈希 = ``sha256(原始 body).hexdigest()``（**小写 hex**）。空 body 时 =
     ``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``。
   - query 段为空时该段就是空串，拼装仍是 `...\n\n\n{hash}`（不省略分隔符）。

2) eop-date 时区 = **北京时区 (UTC+8)**，格式 ``%Y%m%dT%H%M%SZ``（尾部 Z 是字面量）。
   依据：官方文档明确要求"北京时区的当前时间"；官方 Go SDK 用的是 ``time.Now()``
   （**本地**时间，未调 ``.UTC()``），其 ``"20060102T150405Z"`` 布局里的 ``Z`` 没有跟
   ``07:00``，因此只是字面量字符。天翼云的机器/CI 都在北京时区，所以 ``now()`` 就等于
   北京墙上时间。反证：若服务端按 UTC 解析并校验，官方 SDK 在任何中国机器上都恒定偏移
   8 小时、永远签名失败——不可能。故服务端把 eop-date 当**北京时区**理解。

3) 密钥派生链（逐字正确）::

       ktime = HMAC-SHA256(key=sk,   msg=eop_date)        # 完整 "20211221T163614Z"
       kAk   = HMAC-SHA256(key=ktime,msg=ak)              # ktime 用原始字节
       kdate = HMAC-SHA256(key=kAk,  msg=yyyymmdd)        # 仅日期部分 "20211221"
       sig   = Base64( HMAC-SHA256(key=kdate, msg=sigture) )

   签名编码是 **base64**（标准表，带 `=` 填充），不是 hex。

4) Eop-Authorization 头格式（无前缀词）::

       Eop-Authorization: {ak} Headers=ctyun-eop-request-id;eop-date Signature={base64}

   注意：国际站某页文档写成 "ctyun-eop-ak Header=... Signature=..."，那是文档笔误；
   官方 Go SDK 源码是裸 ``{ak}`` + 复数 ``Headers=``，以源码为准。

5) 成功判定：**body 的 ``statusCode == "800"``（JSON 里可能是 int 800 或字符串 "800"）**，
   且 HTTP 状态码为 2xx。仅 HTTP 200 不足以判定成功。HTTP 401 表示鉴权失败。

============================================================================
端点结论（天翼云没有统一的 global 域名，各产线各自一个）
============================================================================
- ECS/资源池产线（ctecs）**有 global 汇聚域名**：``ctecs-global.ctapi.ctyun.cn``
  （官方 Go SDK ``ctecs/const.go``: ``UrlProdCtecs = "ctecs-global.ctapi.ctyun.cn"``）。
  ``GET /v4/region/list-regions`` 正是走这个域名。
- **实测确认**（用无效 AK 发起真实只读请求）::

      GET https://ctecs-global.ctapi.ctyun.cn/v4/region/list-regions   → HTTP 401
      {"eopErrCode":"10002","statusCode":"CTAPI_10002","code":"10002",
       "message":"ctyun-EOP: Invalid APP ID in request:cannot find consumer:<AK>", ...}

  host 解析到天翼云 WAF（203.176.94.86，CNAME 至 ctyunsafewaf/ctbcdn）。请求被正确
  路由到 EOP 网关并解析出 AK（"cannot find consumer:<AK>"），说明端点存活、请求形态
  正确。兄弟产线域名（``ctiam-global`` / ``ctvpc-global`` / ``ctebs-global``）同源存活，
  但对 ``/v4/region/list-regions`` 返回 404（"API Not Found"），佐证该接口归 ctecs。
- 区域型端点形如 ``{product}-{regionCode}.ctapi.ctyun.cn``（例：国际站"云电脑"
  文档给的 ``ecpc-<regionCode>.ctapi.ctyun.cn``）。list-regions 是全局调用，
  用 global 域名即可，**不需要按区域拼接**。
- ``regionId``：**list-regions 不需要 regionId**。官方 SDK 只加可选查询参数
  ``regionName``（资源池名称，可以留空）；``regionID`` 只出现在**响应**里。
  （对比：``/v4/region/get-zones`` 才需要查询参数 ``regionID``，注意大小写。）

============================================================================
鉴权错误码（官方《请求错误码(OpenAPI)》+ 实测）
============================================================================
  eopErrCode 10000 → HTTP 404  ctyunEOP: API Not Found（接口不存在）
  eopErrCode 10002 → HTTP 401  Invalid APP ID in request（AK 在 EOP 系统中不存在）
  eopErrCode 10003 → HTTP 403  签名鉴权失败（SK 错误/签名算法不符）

因此 ``interpret()`` 把 401/403 或 eopErrCode ∈ {10002,10003} 归为 ``bad_cred``，
并尽量区分"AK 不存在"与"签名失败"。

============================================================================
未确定项
============================================================================
- 成功响应里 ``statusCode`` 的确切类型/取值仅由官方 Go SDK 反推为 ``800``
  （``IsSuccess() == (statusCode == "800")``）——本模块按 (int|str) 800 判定，
  并兼容网关可能加的 ``CTAPI_`` 前缀。**未经真实凭据的成功响应验证**。
- 若某些区域/租户要求用区域型端点（非 global），本模块支持 endpoint 模板占位符
  ``{region}``（用 region_id 填充），但该形态未在官方 SDK 中出现，属推测性支持。
- 官方 China 站文档页 (www.ctyun.cn/document/...) 需 JS 渲染，直接抓取得到 404 壳；
  本模块的文档依据来自国际站镜像 (esurfingcloud.com/document/zh-cn/10027004/*)
  与官方 Go SDK 源码。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

__all__ = [
    "DEFAULT_ENDPOINT",
    "LIST_REGIONS_PATH",
    "SUCCESS_STATUS_CODE",
    "SIGNED_HEADERS",
    "beijing_now",
    "eop_date_str",
    "build_signature_string",
    "build_authorization",
    "sign_probe",
    "interpret",
    "probe",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: ECS/资源池产线（ctecs）生产环境 global 端点（官方 Go SDK ctecs/const.go）
DEFAULT_ENDPOINT = "ctecs-global.ctapi.ctyun.cn"

#: 只读探针路径（官方 Go SDK region_list.go：Method=GET, UrlPath=/v4/region/list-regions）
LIST_REGIONS_PATH = "/v4/region/list-regions"

#: 成功状态码（官方 Go SDK response.go：StatusCodeSuccessString）
SUCCESS_STATUS_CODE = "800"

#: 强制参与签名的请求头，按字典序（官方文档："EOP 强制要求 ctyun-eop-request-id 和 eop-date"）
SIGNED_HEADERS = ("ctyun-eop-request-id", "eop-date")

#: 北京时区（eop-date 要求北京时区）
_BEIJING = timezone(timedelta(hours=8))

#: 浏览器 UA。官方 Go SDK 有 AddUserAgentHttpHook，注释写"不添加请求头会出现被风控的现象"，
#: 所以探针也带一个普通浏览器 UA（此头**不参与签名**）。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/83.0.4103.116 Safari/537.36"
)

#: 空 body 的 sha256 十六进制（小写）
_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    """HMAC-SHA256，返回原始 digest 字节。key 用原始字节（base64 解码后的）。"""
    return hmac.new(key, msg, hashlib.sha256).digest()


def _b(x) -> bytes:
    return x.encode("utf-8") if isinstance(x, str) else bytes(x)


def beijing_now() -> datetime:
    """当前北京时间（UTC+8），与宿主机时区无关。"""
    return datetime.now(timezone.utc).astimezone(_BEIJING)


def eop_date_str(now: datetime | None = None) -> str:
    """生成 eop-date：``%Y%m%dT%H%M%SZ``，尾部 Z 为字面量，取北京时区墙上时间。

    与官方 Go SDK ``tim.Format("20060102T150405Z")`` 在传入北京时间时逐字一致。
    """
    now = now or beijing_now()
    if now.tzinfo is not None:
        now = now.astimezone(_BEIJING)
    return now.strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# 待签串 / 签名
# ---------------------------------------------------------------------------

def build_signature_string(eop_date: str, request_id: str, query: str, body: bytes) -> str:
    """拼装待签字符串 sigture。

    格式（与官方 Go SDK ``GetSign`` 的 ``fmt.Sprintf`` 逐字一致）::

        ctyun-eop-request-id:{request_id}
        eop-date:{eop_date}
        <空行>
        {query}
        {body_sha256_hex}

    即 ``"ctyun-eop-request-id:RID\\neop-date:DATE\\n\\nQUERY\\nBODYHASH"``。
    """
    body_hex = hashlib.sha256(body).hexdigest()  # 小写 hex，空 body 亦是
    header_block = "\n".join(f"{name}:{value}" for name, value in (
        ("ctyun-eop-request-id", request_id),
        ("eop-date", eop_date),
    ))  # 已按字典序
    # header 段 + "\n"（分隔）→ 与 header 段自身结构拼出空行；再 query；再 body hash
    return f"{header_block}\n\n{query}\n{body_hex}"


def build_authorization(ak: str, sk: str, eop_date: str, request_id: str,
                        query: str, body: bytes) -> str:
    """计算 ``Eop-Authorization`` 头的值。

    密钥派生链（官方 Go SDK）::

        ktime = HMAC(sk, eop_date)          # 完整 "20211221T163614Z"
        kAk   = HMAC(ktime, ak)
        kdate = HMAC(kAk, yyyymmdd)         # 仅日期部分
        sig   = Base64(HMAC(kdate, sigture))

    返回 ``"{ak} Headers=ctyun-eop-request-id;eop-date Signature={base64}"``。
    """
    sigture = build_signature_string(eop_date, request_id, query, body)
    yyyymmdd = eop_date.split("T", 1)[0]

    k_time = _hmac_sha256(_b(sk), _b(eop_date))
    k_ak = _hmac_sha256(k_time, _b(ak))
    k_date = _hmac_sha256(k_ak, _b(yyyymmdd))
    signature = base64.b64encode(_hmac_sha256(k_date, _b(sigture))).decode("ascii")

    return f"{ak} Headers=ctyun-eop-request-id;eop-date Signature={signature}"


# ---------------------------------------------------------------------------
# 探针构造
# ---------------------------------------------------------------------------

def sign_probe(ak: str, sk: str, *, endpoint: str = DEFAULT_ENDPOINT, region_id: str = "",
               region_name: str = "", request_id: str | None = None,
               eop_date: str | None = None, body: bytes = b"",
               path: str = LIST_REGIONS_PATH) -> tuple:
    """构造一个已签名的只读请求。

    参数
    ----
    ak, sk : 天翼云 AccessKey / SecurityKey（各 32 位）。
    endpoint : 端点 host。可以是完整 host（默认 ``ctecs-global.ctapi.ctyun.cn``），
        也可以含 ``{region}`` 占位符（用 ``region_id`` 填充），以支持区域型端点。
    region_id : 资源池 ID。**list-regions 本身不需要 regionId**（官方 SDK/文档均无此
        请求参数）；此处仅用于 ``endpoint`` 里的 ``{region}`` 占位符替换。
    region_name : 可选查询参数 ``regionName``（官方 SDK 唯一入参，可为空）。
        为空时**不带任何 query**（等价于查询全部资源池）。
    request_id, eop_date, body, path : 测试/高级用途覆盖（默认自动生成）。

    返回
    ----
    ``(method, url, headers, body)``
        method : ``"GET"``
        url    : ``https://{endpoint}{path}[?{query}]``
        headers: 含 ``ctyun-eop-request-id`` / ``Eop-date`` / ``Eop-Authorization`` /
                 ``User-Agent`` 的 dict
        body   : ``bytes``（GET 为空）
    """
    if endpoint and "{region}" in endpoint:
        endpoint = endpoint.replace("{region}", region_id or "")
    endpoint = endpoint.rstrip("/")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        base = endpoint
    else:
        base = "https://" + endpoint

    # query：官方 SDK 仅 regionName（可选）。为空则不带 query。
    query = urlencode({"regionName": region_name}) if region_name else ""

    request_id = request_id or str(_uuid.uuid4())
    eop_date = eop_date or eop_date_str()

    authorization = build_authorization(ak, sk, eop_date, request_id, query, body)

    headers = {
        "ctyun-eop-request-id": request_id,
        "Eop-date": eop_date,
        "Eop-Authorization": authorization,
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }

    url = base + path + (("?" + query) if query else "")
    return "GET", url, headers, body


# ---------------------------------------------------------------------------
# 结果判定
# ---------------------------------------------------------------------------

def _as_code_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


def _normalize_code(s: str) -> str:
    """归一化状态码用于比较：去掉下划线/连字符并大写。

    网关错误信封里的 statusCode 实测是 ``"CTAPI_10002"``（官方文档样例写成
    ``"CTAPI10002"``，两处不一致），故归一化后再比较。
    """
    return (s or "").replace("_", "").replace("-", "").strip().upper()


#: 归一化后的成功码集合（"800" / "CTAPI800"）
_SUCCESS_CODES = {"800", "CTAPI800"}

#: 鉴权类网关错误码（官方《请求错误码(OpenAPI)》）：
#:   10002 → HTTP 401，"Invalid APP ID in request"（ctyun-eop-ak 在 EOP 系统中不存在）
#:   10003 → HTTP 403，签名鉴权失败
_AUTH_ERR_CODES = {"10002", "10003"}


def interpret(status_code: int, body_text: str) -> dict:
    """解析探针响应，返回 ``{"verdict", "message"}``。

    verdict 取值：
      * ``"ok"``        凭据有效（HTTP 2xx 且 body ``statusCode == 800``）
      * ``"bad_cred"``  鉴权失败（HTTP 401/403，或业务 errorCode 指向 AK/签名）
      * ``"error"``     其它（网络/服务端/接口不存在/参数错误等）

    成功判定以官方 Go SDK 为准：HTTP 2xx **且** body 的 ``statusCode == 800``
    （官方 SDK ``CtyunResponseModel.IsSuccess()``）。

    网关错误信封（实测）形如::

        {"eopErrCode":"10002","error":"10002","statusCode":"CTAPI_10002",
         "code":"10002","message":"ctyun-EOP: Invalid APP ID in request:...",
         "returnObj":{},"requestId":"..."}
    """
    body_text = body_text or ""
    data = None
    try:
        data = json.loads(body_text) if body_text.strip() else None
    except (ValueError, TypeError):
        data = None

    status = code = error_code = eop_err = message = description = ""
    if isinstance(data, dict):
        status = _as_code_text(data.get("statusCode"))
        code = _as_code_text(data.get("code"))
        error_code = _as_code_text(data.get("errorCode"))
        eop_err = _as_code_text(data.get("eopErrCode") or data.get("error"))
        message = (data.get("message") or data.get("errormsg") or "").strip()
        description = (data.get("description") or "").strip()

    detail = " | ".join(x for x in (
        f"http={status_code}",
        f"statusCode={status}" if status else "",
        f"eopErrCode={eop_err}" if eop_err else "",
        f"message={message}" if message else "",
        f"description={description}" if description else "",
    ) if x)

    norm_status = _normalize_code(status)
    numeric_errs = {_as_code_text(eop_err), _as_code_text(code), _as_code_text(error_code)}
    blob = f"{eop_err} {code} {error_code} {status} {message} {description}".lower()

    # 1) 明确成功：HTTP 2xx 且 statusCode == 800
    if 200 <= int(status_code) < 300 and norm_status in _SUCCESS_CODES:
        return {"verdict": "ok", "message": f"凭据有效，资源池列表已返回（{detail}）"}

    # 2) 鉴权失败：HTTP 401/403，或 EOP 鉴权错误码
    if status_code in (401, 403) or (numeric_errs & _AUTH_ERR_CODES):
        if "10002" in numeric_errs or "app id" in blob or "consumer" in blob:
            why = "AK 在 EOP 系统中不存在（eopErrCode=10002）"
        elif "10003" in numeric_errs or "signature" in blob or "签名" in blob:
            why = "签名校验失败（SK 错误或签名算法不符，eopErrCode=10003）"
        else:
            why = "AK/SK 无效或签名不匹配"
        return {"verdict": "bad_cred", "message": f"鉴权失败：{why}（{detail}）"}

    # 3) 网关以 200 返回但 statusCode 非成功码
    if 200 <= int(status_code) < 300 and status and norm_status not in _SUCCESS_CODES:
        return {"verdict": "error", "message": f"接口返回非成功状态码（{detail}）"}

    # 4) 接口不存在 / 其它
    snippet = body_text.strip().replace("\n", " ")[:200]
    if status_code == 404 or "10000" in numeric_errs or "not found" in blob:
        return {"verdict": "error",
                "message": f"接口不存在或路径错误（{detail}）: {snippet}"}
    return {"verdict": "error",
            "message": (f"异常响应（{detail}）: {snippet}" if status or eop_err
                        else f"HTTP {status_code}，非预期响应: {snippet}")}


# ---------------------------------------------------------------------------
# 自测：用官方 Go SDK 生成的测试向量逐字比对
# ---------------------------------------------------------------------------

# 由官方 SDK（ctyun-it/ctyun-sdk-go, GetSign）在固定入参下真实生成：
#   AK = "AKID0123456789ABCDEF0123456789AB"
#   SK = "SK0123456789abcdef0123456789ABCD"
#   request_id = "33dfa732-b27b-464f-b15a-21ed6845afd5"
#   date = "20211221T163614Z"   (time.Date(2021,12,21,16,36,14,0,time.UTC))
_GOLDEN_AK = "AKID0123456789ABCDEF0123456789AB"
_GOLDEN_SK = "SK0123456789abcdef0123456789ABCD"
_GOLDEN_RID = "33dfa732-b27b-464f-b15a-21ed6845afd5"
_GOLDEN_DATE = "20211221T163614Z"

_GOLDEN_VECTORS = [
    # (query, body, expected Eop-Authorization)
    ("", b"",
     "AKID0123456789ABCDEF0123456789AB Headers=ctyun-eop-request-id;eop-date "
     "Signature=tAP+0CfG7Y7Hxul6PfESj4FgdtBt6faQc4mU1fAIZhk="),
    ("regionName=", b"",
     "AKID0123456789ABCDEF0123456789AB Headers=ctyun-eop-request-id;eop-date "
     "Signature=wR2FKzs0s6LNilQLgFhhuMFFOx0+qOmYyt7rJLcHHMg="),
    ("regionName=%E5%8C%97%E4%BA%AC", b"",
     "AKID0123456789ABCDEF0123456789AB Headers=ctyun-eop-request-id;eop-date "
     "Signature=wc1Mt5+QYcaoFlDyBgSQwgJqAitNeyybEiMQdUexvrI="),
    ("", b'{"regionId":"cn-north-1"}',
     "AKID0123456789ABCDEF0123456789AB Headers=ctyun-eop-request-id;eop-date "
     "Signature=Z1v/W4cnOOhW6gVnW1bl4+2mwnvIW9C9+EqNKw4difA="),
]

#: 空 body 的 sha256（官方文档示例值）
_DOC_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _self_test() -> int:
    ok = 0
    total = 0

    def check(name, cond, got="", want=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            print(f"  [FAIL] {name}\n         got : {got}\n         want: {want}")

    print("== 1. 空 body 哈希 ==")
    check("sha256(b'') 与官方文档一致", _EMPTY_BODY_SHA256 == _DOC_EMPTY_SHA256,
          _EMPTY_BODY_SHA256, _DOC_EMPTY_SHA256)

    print("== 2. 待签串拼装（双换行）==")
    ss = build_signature_string(_GOLDEN_DATE, _GOLDEN_RID, "regionName=", b"")
    want_ss = (
        f"ctyun-eop-request-id:{_GOLDEN_RID}\n"
        f"eop-date:{_GOLDEN_DATE}\n"
        f"\n"
        f"regionName=\n"
        f"{_DOC_EMPTY_SHA256}"
    )
    check("sigture 结构与官方 Go fmt.Sprintf 一致", ss == want_ss, repr(ss), repr(want_ss))

    print("== 3. 官方 Go SDK 测试向量逐字比对 ==")
    for i, (q, b, expected) in enumerate(_GOLDEN_VECTORS, 1):
        got = build_authorization(_GOLDEN_AK, _GOLDEN_SK, _GOLDEN_DATE, _GOLDEN_RID, q, b)
        check(f"vector-{i} query={q!r} body={b!r}", got == expected, got, expected)

    print("== 4. eop-date 时区 ==")
    from datetime import datetime as _dt
    d = eop_date_str(_dt(2021, 12, 21, 16, 36, 14, tzinfo=timezone.utc))
    # 16:36:14 UTC → 北京次日 00:36:14
    check("UTC 输入被转换为北京时区", d == "20211222T003614Z", d, "20211222T003614Z")

    print("== 5. sign_probe 结构 ==")
    method, url, headers, body = sign_probe(
        _GOLDEN_AK, _GOLDEN_SK, endpoint=DEFAULT_ENDPOINT,
        region_id="", request_id=_GOLDEN_RID, eop_date=_GOLDEN_DATE)
    check("method=GET", method == "GET", method, "GET")
    check("url 正确", url == "https://ctecs-global.ctapi.ctyun.cn/v4/region/list-regions",
          url, "https://ctecs-global.ctapi.ctyun.cn/v4/region/list-regions")
    check("含 Eop-Authorization",
          headers.get("Eop-Authorization") == _GOLDEN_VECTORS[0][2],
          headers.get("Eop-Authorization"), _GOLDEN_VECTORS[0][2])
    check("Eop-date 头 = 输入", headers.get("Eop-date") == _GOLDEN_DATE)
    check("body 为空 bytes", body == b"")

    method, url, headers, _ = sign_probe(
        _GOLDEN_AK, _GOLDEN_SK, endpoint="ctecs-{region}.ctapi.ctyun.cn",
        region_id="cn-north-1", region_name="北京",
        request_id=_GOLDEN_RID, eop_date=_GOLDEN_DATE)
    check("endpoint {region} 占位符替换",
          url == "https://ctecs-cn-north-1.ctapi.ctyun.cn/v4/region/list-regions"
          "?regionName=%E5%8C%97%E4%BA%AC", url)
    check("regionName 出现在待签串中（签名随 query 变化）",
          headers.get("Eop-Authorization") != _GOLDEN_VECTORS[0][2])

    print("== 6. interpret 判定 ==")
    r = interpret(200, json.dumps({"statusCode": 800, "message": "", "returnObj": {}}))
    check("statusCode=800 → ok", r["verdict"] == "ok", r["verdict"], "ok")
    r = interpret(200, json.dumps({"statusCode": "800"}))
    check("statusCode='800' 字符串 → ok", r["verdict"] == "ok", r["verdict"], "ok")
    # 真实网关信封（实测）：AK 不存在
    _real_401 = ('{"eopErrCode":"10002","error":"10002","requestId":"x",'
                 '"statusCode":"CTAPI_10002","message":"ctyun-EOP: Invalid APP ID in '
                 'request:cannot find consumer:AK","returnObj":{},"code":"10002"}')
    r = interpret(401, _real_401)
    check("实测信封 401/10002 → bad_cred", r["verdict"] == "bad_cred", r["verdict"], "bad_cred")
    check("10002 归因为 AK 不存在", "AK" in r["message"] and "10002" in r["message"], r["message"])
    # 签名失败（官方错误码表：10003 → HTTP 403）
    r = interpret(403, json.dumps({"eopErrCode": "10003", "statusCode": "CTAPI_10003",
                                   "message": "signature verify failed"}))
    check("403/10003 → bad_cred(签名)", r["verdict"] == "bad_cred"
          and "签名" in r["message"], r["verdict"], "bad_cred")
    r = interpret(200, json.dumps({"statusCode": 900, "errorCode": "SomeBizError",
                                   "message": "param invalid"}))
    check("200+业务错误码 → error", r["verdict"] == "error", r["verdict"], "error")
    r = interpret(404, json.dumps({"eopErrCode": "10000", "statusCode": "CTAPI_10000",
                                   "message": "ctyunEOP: API Not Found"}))
    check("404/10000 → error(接口不存在)", r["verdict"] == "error"
          and "不存在" in r["message"], r["verdict"], "error")
    r = interpret(500, "<html>502 Bad Gateway</html>")
    check("HTTP 500 → error", r["verdict"] == "error", r["verdict"], "error")

    print(f"\n== 结果: {ok}/{total} 通过 ==")
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
