# 签名器校准证据

`plugins/cloud/` 下五家云厂商（天翼云 / 京东云 / UCloud / 火山引擎 / 百度智能云）的只读探针
**不依赖官方 SDK**，各自带一份自包含签名器。签名算法不是猜的，是对着**官方 SDK 源码逐字校准**出来的。

本文件保存这些**逐字证据**（源码摘录 + `文件:行` + 版本 / commit）。签名器模块的 docstring
**只留结论**，依据（源码原样）都在这里。**改动任何一个签名算法之前，先读本文件。**

约定：

- 摘录均为**原样拷贝**（含 `\n` 这类字面量），`文件:行` 指向校准时点的快照。
- 版本 / commit 是**校准时点**；官方 SDK 之后若改动，需按下方「重新校准」重新比对。
- 结论与固定向量（黄金向量）保留在各签名器模块里，随代码走。
- 各厂商的 SDK 快照在本仓库 `tmp/` 下（`tmp/ctyun-sdk-go`、`tmp/jdcloud_sdk_src`、
  `tmp/ucloud_sdk`、`tmp/bce_sdk_src`），便于复现比对。

---

## 1. 天翼云 EOP（`plugins/cloud/ctyun/eop_signer.py`）

天翼云走自研 **EOP 网关**（EOP = 天翼云统一开放平台），签名与阿里云 / 腾讯云 / 华为云都不同源，
与 AWS SigV4 也没有亲缘关系。

### 校准基准

官方 **Go** SDK `ctyun-it/ctyun-sdk-go`（快照：`tmp/ctyun-sdk-go`）：

- `ctyun-sdk-core/request.go` —— 待签串拼装与密钥派生（`GetSign` / `hmacSHA256` / `buildRequest`）
- `ctyun-sdk-core/response.go` —— 成功判定
- `ctyun-sdk-endpoint/ctecs/const.go`、`region_list.go` —— 端点与入参

> 天翼云**没有官方 Python SDK**（PyPI 上的 `ctyun` 是无关的库存管理包；GitHub 上 `fatelei/pyctyun`
> 是第三方）。能对标的只有官方 Go SDK 源码与文档。

### 逐字摘录

`ctyun-sdk-core/request.go:117-132` —— 待签串拼装与密钥派生 `GetSign()`：

```go
// GetSign 加签
func GetSign(query string, body []byte, tim time.Time, uuid string, credential Credential) string {
	hash := sha256.New()
	hash.Write(body)
	sum := hash.Sum(nil)
	calculateContentHash := hex.EncodeToString(sum)
	date := tim.Format("20060102T150405Z")
	sigture := fmt.Sprintf("ctyun-eop-request-id:%s\neop-date:%s\n\n%s\n%s", uuid, date, query, calculateContentHash)
	singerDd := tim.Format("20060102")
	kAk := hmacSHA256(credential.ak, string(hmacSHA256(date, credential.sk)))
	kdate := hmacSHA256(singerDd, string(kAk))
	signaSha256 := hmacSHA256(sigture, string(kdate))
	Signature := base64.StdEncoding.EncodeToString(signaSha256)
	signHeader := credential.ak + " Headers=ctyun-eop-request-id;eop-date Signature=" + Signature
	return signHeader
}
```

`ctyun-sdk-core/request.go:134-142` —— HMAC 单元，**注意入参顺序（第一个是消息，第二个是密钥）**：

```go
// hmacSHA256 HmacSHA256加密
func hmacSHA256(signature string, key string) []byte {
	s := []byte(signature)
	k := []byte(key)
	m := hmac.New(sha256.New, k)
	m.Write(s)
	sum := m.Sum(nil)
	return sum
}
```

`ctyun-sdk-core/request.go:95-107` —— `buildRequest()` 组装请求头（时间 / 请求 ID / 授权头）：

```go
	// 构造请求头
	tim := time.Now()
	eopDate := tim.Format("20060102T150405Z")
	id := uuid.NewString()
	sign := GetSign(u.RawQuery, c.body, tim, id, c.credential)
	headers := c.headers.Clone()
	headers.Add("ctyun-eop-request-id", id)
	headers.Add("Eop-Authorization", sign)
	headers.Add("Eop-date", eopDate)
	if c.body != nil {
		headers.Add("Content-Length", strconv.Itoa(len(c.body)))
	}
```

`ctyun-sdk-core/response.go:14-16,31-34` —— 成功判定：

```go
const (
	StatusCodeSuccessString = "800"
)
```

```go
// IsSuccess 判断响应是否成功
func (c CtyunResponseModel) IsSuccess() bool {
	return c.ParseStatusCode() == StatusCodeSuccessString
}
```

### 黄金向量（接受测试）

`tmp/ctyungo/main.go` 是官方 Go SDK 的接受测试骨架，直接调 `GetSign()` 打印授权头：

```go
	cred, err := ctyunsdk.NewCredential("AKID0123456789ABCDEF0123456789AB", "SK0123456789abcdef0123456789ABCD")
	tim := time.Date(2021, 12, 21, 16, 36, 14, 0, time.UTC)
	const rstID = "33dfa732-b27b-464f-b15a-21ed6845afd5"
	cases := []struct{ q, body string }{
		{"", ""},
		{"regionName=", ""},
		{"regionName=%E5%8C%97%E4%BA%AC", ""},
		{"", `{"regionId":"cn-north-1"}`},
	}
	for _, c := range cases {
		s := ctyunsdk.GetSign(c.q, []byte(c.body), tim, rstID, *cred)
		fmt.Printf("QUERY=%q BODY=%q\n%s\n", c.q, c.body, s)
	}
```

### 钉死的结论（摘要）

1. **待签串**（`\n` 为字面换行）：
   `"ctyun-eop-request-id:{uuid}\neop-date:{eop_date}\n\n{query}\n{body_sha256_hex}"`
   - header 段按 key 升序、逐行 `key:value\n`；header 段与 query 段之间是**一个空行**（两个 `\n`）；
     query 段与 body 段之间一个 `\n`；
   - body 哈希 = `sha256(原始 body).hexdigest()`（小写 hex）；空 body 时为
     `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；
   - **只签两个头**：`ctyun-eop-request-id`、`eop-date`。
2. **`eop-date` 取北京时区（UTC+8）**，格式 `%Y%m%dT%H%M%SZ`（尾部 `Z` 是字面量，不是 UTC 标记）。
   依据：`buildRequest` 用 `time.Now()`（**本地**时间，未调 `.UTC()`），而布局串里的 `Z` 没跟 `07:00`。
   反证：若服务端按 UTC 解析，官方 SDK 在任何中国机器上都恒定偏移 8 小时、永远签名失败——不可能。
3. **密钥派生链**：
   - `ktime = HMAC-SHA256(key=sk,    msg=eop_date)`
   - `kAk   = HMAC-SHA256(key=ktime, msg=ak)`
   - `kdate = HMAC-SHA256(key=kAk,   msg=yyyymmdd)`
   - `sig   = Base64(HMAC-SHA256(key=kdate, msg=待签串))`
4. **授权头**（无算法前缀词）：`Eop-Authorization: {ak} Headers=ctyun-eop-request-id;eop-date Signature={base64}`
   （国际站某页写成 `ctyun-eop-ak Header=...`，是文档笔误；以 Go 源码为准。）
5. **成功判定不是 HTTP 200**：body 的 `statusCode == "800"`（int 或 str）**且** HTTP 为 2xx；
   HTTP 401 表示鉴权失败。

### 端点结论

- ECS/资源池产线（ctecs）有 global 汇聚域名：`ctecs-global.ctapi.ctyun.cn`
  （`ctyun-sdk-endpoint/ctecs/const.go`：`UrlProdCtecs`）。`GET /v4/region/list-regions` 走它。
- **实测**（无效 AK 发起真实只读请求）→ HTTP 401，响应体逐字：

```json
{"eopErrCode":"10002","statusCode":"CTAPI_10002","code":"10002",
 "message":"ctyun-EOP: Invalid APP ID in request:cannot find consumer:<AK>", ...}
```

  host 解析到天翼云 WAF（`ctyunsafewaf` / `ctbcdn`），请求被正确路由到 EOP 网关并解析出 AK
  （`cannot find consumer:<AK>`），说明端点存活、请求形态正确。
- `regionId`：**list-regions 不需要 regionId**；官方 SDK 只加可选查询参数 `regionName`。
  对比 `/v4/region/get-zones` 才需要查询参数 `regionID`（注意大小写）。

### 鉴权错误码

| `eopErrCode` | HTTP | 含义 |
|---|---|---|
| 10000 | 404 | `ctyunEOP: API Not Found`（接口不存在） |
| 10002 | 401 | `Invalid APP ID in request`（AK 在 EOP 系统中不存在） |
| 10003 | 403 | 签名鉴权失败（SK 错误 / 签名算法不符） |

### 未确定项

- 成功响应里 `statusCode` 的确切类型/取值仅由 Go SDK 反推为 `800`，**未经真实凭据验证**。
- `{region}` 端点模板属推测性支持（官方 SDK 中未出现该形态）。
- 除 `GET /v4/region/list-regions` 外，其余接口的响应体字段层次与入参形态未经实测。

### 参考文档

- https://www.ctyun.cn/document/10026730/10040588
- https://www.esurfingcloud.com/document/zh-cn/10027004/10028368
- https://www.esurfingcloud.com/document/zh-cn/10027004/10028371

---

## 2. 京东云 JDCLOUD3（`plugins/cloud/jdcloud/jdcloud_signer.py`）

### 校准基准

离线解包官方 Python SDK（非记忆、非二手博客）：`pip download jdcloud-sdk` → **PyPI 1.6.346**
（快照：`tmp/jdcloud_sdk_src/jdcloud_sdk-1.6.346`）。比对 `jdcloud_sdk/core/` 下的
`signer.py`、`const.py`、`jdcloudresponse.py`。

### 逐字摘录

`jdcloud_sdk/core/const.py:17-22` —— 常量：

```python
JDCLOUD2 = 'JDCLOUD2'
JDCLOUD3 = 'JDCLOUD3'
JDCLOUD_ALGORITHM = 'JDCLOUD2-HMAC-SHA256'
JDCLOUD3_ALGORITHM = 'JDCLOUD3-HMAC-SHA256'
JDCLOUD_REQUEST = 'jdcloud2_request'
JDCLOUD3_REQUEST = 'jdcloud3_request'
```

`jdcloud_sdk/core/signer.py:167-175` —— 四段 HMAC 派生链（**`JDCLOUD3` 前缀拼在 key 上**）：

```python
    def __sign(self, key, msg):
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

    def __get_signature_key(self, key, date_stamp, region_name, service_name):
        k_date = self.__sign((const.JDCLOUD3 + key).encode('utf-8'), date_stamp)
        k_region = self.__sign(k_date, region_name)
        k_service = self.__sign(k_region, service_name)
        k_signing = self.__sign(k_service, const.JDCLOUD3_REQUEST)
        return k_signing
```

`jdcloud_sdk/core/signer.py:70-92` —— 待签串、签名与授权头（中间两行日志已略去）：

```python
        algorithm = const.JDCLOUD3_ALGORITHM
        credential_scope = (date_str + '/' +
                            region + '/' +
                            service + '/' +
                            const.JDCLOUD3_REQUEST)
        string_to_sign = (algorithm + '\n' +
                          jdcloud_date + '\n' +
                          credential_scope + '\n' +
                          self.__sha256_hash(canonical_request))
        # ...（略去两行 logger 输出）
        signing_key = self.__get_signature_key(credential.secret_key, date_str, region, service)
        encoded = string_to_sign.encode('utf-8')
        signature = hmac.new(signing_key, encoded, hashlib.sha256).hexdigest()

        authorization_header = (
                algorithm + ' ' +
                'Credential=' + credential.access_key + '/' + credential_scope + ', ' +
                'SignedHeaders=' + signed_headers + ', ' +
                'Signature=' + signature
        )
```

`jdcloud_sdk/core/jdcloudresponse.py:33-45` —— 错误体解析（`error` 外层信封）：

```python
    def load_hook(self, resp_dict):
        request_id = resp_dict.get('requestId')
        if request_id:
            self.request_id = request_id

        error = resp_dict.get('error')
        if request_id and error:
            self.error = ErrorResponse(error.get('status'), error.get('code'), error.get('message'))

        result = resp_dict.get('result')
        if request_id and result:
            self.result = resp_dict['result']
        return resp_dict
```

### 结论：采用 JDCLOUD3

- `JDCLOUD2` 是**已废弃的旧版方案**；readthedocs 的 `/en/pydoc2/` 是陈旧文档（确为
  `const.JDCLOUD2 + key`），**不采用**。
- 算法标识 `JDCLOUD3-HMAC-SHA256`、签名前缀 `JDCLOUD3`、scope 后缀 `jdcloud3_request`
  （对应模块里的 `ALGORITHM` / `_SIGN_PREFIX` / `_SCOPE_SUFFIX`）。
- 函数名仍叫 `signV3`，与常量 `JDCLOUD3` 指同一套现行算法。
- 固定向量取自 SDK 自带 `signer_test.py`，离线可跑。

### 参考文档

- https://github.com/jdcloud-api/jdcloud-sdk-python
- https://docs.jdcloud.com/cn/iam/signature
- https://docs.jdcloud.com/cn/virtual-machines/api/describeinstances
- https://docs.jdcloud.com/cn/cloud-disk-service/api/describedisks

---

## 3. UCloud（`plugins/cloud/ucloud/ucloud_signer.py`）

### 校准基准

- 官方 Python SDK `ucloud/ucloud-sdk-python3` @ commit `cc58bf1`，**v0.11.145**（2026-10-09）
  （快照：`tmp/ucloud_sdk/gh_sdk`；`ucloud/version.py` → `version = "0.11.145"`）：
  - `ucloud/core/auth/_cfg.py` —— `verify_ac()` 签名算法本体
  - `ucloud/core/typesystem/encoder.py` —— `encode_value()` 值编码（无 URL 转义）
  - `tests/test_unit/test_core/test_auth.py` —— 官方固定测试向量
- 官方文档仓库 `UCloudDoc-Team/api`：`summary/public.md`（公共参数 / 错误码）、`summary/signature`（签名算法）。

### 逐字摘录

`ucloud/core/auth/_cfg.py:28-38` —— 签名算法本体：

```python
    params = OrderedDict(sorted(params.items(), key=lambda item: item[0]))

    simplified = ""
    for key, value in params.items():
        simplified += str(key) + encoder.encode_value(value)
    simplified += private_key

    hash_new = hashlib.sha1()
    hash_new.update(simplified.encode("utf-8"))
    hash_value = hash_new.hexdigest()
    return hash_value
```

`ucloud/core/auth/_cfg.py:18-19` —— 文件自带的官方固定向量（doctest）：

```
>>> verify_ac("my_private_key", {"foo": "bar"})
'634edc1bb957c0d65e5ab5494cf3b7784fbc87af'
```

`ucloud/core/auth/_cfg.py:56,62-64` —— `PublicKey` 通过 `verify_ac` 注入、**参与签名**：

```python
    PUBLIC_KEY_NAME = "PublicKey"

    def verify_ac(self, args: dict) -> str:
        args[Credential.PUBLIC_KEY_NAME] = self.public_key
        return verify_ac(self.private_key, args)
```

`ucloud/core/typesystem/encoder.py:24-33` —— 值编码（除 bool/float 外一律 `str(v)`，**无 URL 转义**）：

```python
def encode_value(v):
    # bool only accept lower case
    if isinstance(v, bool):
        return "true" if v else "false"

    # api gateway will try to decode float as int in lua syntax
    if isinstance(v, float):
        return str(int(v)) if v % 1 == 0 else str(v)

    return str(v)
```

### 钉死的结论

- 摘要算法 **SHA1**，输出 **hex**（40 位小写），**不是 base64**。
- 拼接串 **不做任何 URL / HTTP 转义**；**键值之间无分隔符**：`<key><value><key><value>...`。
- 参数按键名**升序**；`PublicKey` **参与**签名，`Signature` 不参与；PrivateKey **原样**追加在末尾。
- 请求方式取 **POST + JSON body**（官方文档支持 GET/POST × json/x-www-form-urlencoded 四种组合；
  官方 Python SDK 默认 `POST + x-www-form-urlencoded`，服务端两种都收）。

### 参考文档

- https://docs.ucloud.cn/api/summary/signature
- https://docs.ucloud.cn/api/summary/public
- https://github.com/ucloud/ucloud-sdk-python3

---

## 4. 火山引擎 SignerV4（`plugins/cloud/volcengine/volc_signer.py`）

### 校准基准

按官方 Python SDK `volcengine/volc-sdk-python` 的 `volcengine/auth/SignerV4.py` **逐行对齐**，
并由官方示例 `example/DemoSignOnly.py` 的固定向量校准（模块末尾自测可跑）。

> 本仓库**未留存**该 SDK 的快照（其余四家都有 `tmp/` 快照）。**重新校准**时需重新拉取
> `volc-sdk-python`，把 `SignerV4.py` 与本模块逐行 diff，并复跑 `DemoSignOnly.py` 的向量。

### 结论（与 AWS SigV4 的差异）

- `Authorization` 前缀是 `HMAC-SHA256`，**不是** `AWS4-HMAC-SHA256`。
- `kDate = HMAC-SHA256(SK, date)` 用的是**裸 SK**（不加 `"AWS4"` 前缀）。
- 头名精确为 `X-Date` / `X-Content-Sha256`；`X-Security-Token` **仅在** STS 临时凭证
  （`session_token` 非空）时才出现，纯 AK/SK 时不存在。
- `SignedHeaders` 只收 `Host` / `Content-Type` / `Content-Md5` 以及所有 `X-` 前缀头（小写），
  按字典序以 `;` 连接；GET 探针下即 `host;x-content-sha256;x-date`。
- 凭证范围 = `{yyyymmdd}/{region}/{service}/request`——**地域只体现在 scope 里，不进 query**。
- 模块只产出 `(method, url, headers, body)`，**不发请求**；实际发送必须走
  `backend.plugin_sdk.make_client()`（只有它带得上凭据级代理）。

### Ark 区分

火山**方舟 Ark / 豆包大模型**（`ark.cn-beijing.volces.com`，`Authorization: Bearer <API Key>`）
与本文的 **OpenAPI 网关签名**是两回事，**不共用**本签名器。

### 参考文档

- https://github.com/volcengine/volc-sdk-python/blob/main/volcengine/auth/SignerV4.py
- https://www.volcengine.com/docs/6491/1185994

---

## 5. 百度智能云 BCE（`plugins/cloud/baiducloud/bce_signer.py`）

### 校准基准

官方 SDK `baidubce/bce-sdk-python` @ commit **`b1979d1e63240b88ef8c68c0456800e0d9ae31bf`**，
`SDK_VERSION = b'0.9.79'`（快照：`tmp/bce_sdk_src`）。比对：

- `baidubce/auth/bce_v1_signer.py` —— `_get_canonical_headers` / `sign`
- `baidubce/utils.py` —— `get_canonical_time` / `normalize_string` / `get_canonical_querystring`
- `baidubce/http/bce_http_client.py` —— `send_request` 里 Host / Content-Length / `x-bce-date` 的先后次序

### 逐字摘录

`baidubce/auth/bce_v1_signer.py:30-50` —— 规范头：

```python
def _get_canonical_headers(headers, headers_to_sign=None):
    headers = headers or {}

    if headers_to_sign is None or len(headers_to_sign) == 0:
        headers_to_sign = set([b"host",
                               b"content-md5",
                               b"content-length",
                               b"content-type"])
    result = []
    for k in headers:
        k_lower = k.strip().lower()
        value = utils.convert_to_standard_string(headers[k]).strip()
        # 值为空字符的header不参与签名计算
        if not value:
            continue
        if k_lower.startswith(http_headers.BCE_PREFIX) \
                or k_lower in headers_to_sign:
            str_tmp = b"%s:%s" % (utils.normalize_string(k_lower), utils.normalize_string(value))
            result.append(str_tmp)
    result.sort()
    return (b'\n').join(result)
```

`baidubce/auth/bce_v1_signer.py:65-89` —— 签名本体（`sign_key` 派生 + 待签串 + 最终串）：

```python
    sign_key_info = b'bce-auth-v1/%s/%s/%d' % (
        credentials.access_key_id,
        utils.get_canonical_time(timestamp),
        expiration_in_seconds)
    sign_key = hmac.new(
        credentials.secret_access_key,
        sign_key_info,
        hashlib.sha256).hexdigest()

    canonical_uri = path
    canonical_querystring = utils.get_canonical_querystring(params, True)

    canonical_headers = _get_canonical_headers(headers, headers_to_sign)
    string_to_sign = (b'\n').join([
        http_method, canonical_uri,
        canonical_querystring, canonical_headers
        ])
    sign_result = hmac.new(compat.convert_to_bytes(sign_key), string_to_sign, hashlib.sha256).hexdigest()
    # convert to bytes
    sign_result = compat.convert_to_bytes(sign_result)

    if headers_to_sign:
        result = b'%s/%s/%s' % (sign_key_info, (b';').join(headers_to_sign), sign_result)
    else:
        result = b'%s//%s' % (sign_key_info, sign_result)
```

`baidubce/utils.py:395-411` —— `x-bce-date` 的规范时间：

```python
def get_canonical_time(timestamp=0):
    ...
    if timestamp == 0:
        utctime = datetime.datetime.utcnow()
    else:
        utctime = datetime.datetime.utcfromtimestamp(timestamp)
    return b"%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        utctime.year, utctime.month, utctime.day,
        utctime.hour, utctime.minute, utctime.second)
```

`baidubce/utils.py:675-691` —— 规范 query：

```python
def get_canonical_querystring(params, for_signature):
    ...
    if params is None:
        return ''
    result = []
    for k, v in iteritems(params):
        if not for_signature or k.lower != http_headers.AUTHORIZATION.lower():
            if v is None:
                v = ''
            result.append(b'%s=%s' % (normalize_string(k), normalize_string(v)))
    result.sort()
    return (b'&').join(result)
```

### 参考文档

- 生成认证字符串：https://cloud.baidu.com/doc/Reference/s/njwvz1yfu
- 签名流程：https://cloud.baidu.com/doc/Reference/s/6k6yfh1he
- IAM API 概述（`iam.bj.baidubce.com`）：https://cloud.baidu.com/doc/IAM/s/Pjx35b92m
- IAM 错误码：https://cloud.baidu.com/doc/IAM/s/Rjx4d0rxo
- 通用错误码（`RequestExpired` 等）：https://cloud.baidu.com/doc/Reference/s/Bjz3uz3qj

---

## 重新校准（当怀疑签名器失效时）

1. 取对应厂商**校准时点之后**的官方 SDK（见各节「校准基准」），落到 `tmp/`。
2. 按本文的 `文件:行` 找到对应函数，与本模块实现逐行比对（本文摘录也应同步刷新）。
3. 跑各签名器模块的 `__main__` 自测（内含黄金向量 / SDK 自带测试向量），离线即可验证。
4. 需要实网确认时，用**无效凭据**发起只读探针：能收回到「凭据无效」类错误码即说明
   端点、请求形态与签名流程正确；收到「签名算法不符 / 请求格式错误」才是签名器出了问题。
