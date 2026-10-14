"""短信网关 / 接码服务商凭据验证插件（plugins/api/ 类别下的通用子插件）。

**为什么字段是通用的**：这批服务商在同一件事上的叫法完全不同（accessKey / appKey / apiKey /
account / clientId / productToken / instanceId …），一家一个插件既冗余又难维护，因此做成
「服务商 + 通用标识/密钥 + 附加参数」的信封，各家命名照实写进 manifest 的 hint 里。

**只做只读校验，绝不发短信**：短信网关的常规校验手段就是「发一条短信看是否成功」，那会真的
投递、产生费用，并可能触发风控（限速、封号）。更麻烦的是这批厂商普遍把**发送接口的参数校验放在
鉴权之前**，用发送接口探活只会拿到「参数错误」，既不能证明有效也不能证明无效。因此本插件只调
**余额 / 模板 / 日志 / 交易**这类只读接口，且**每个判定都经过阴性对照**（乱写凭据看报文是否
真的不同）。找不到只读接口的服务商一律给 ``unknown`` 并说明原因——**不猜、不给假结论**。

各家只读校验接口与判定依据（判定均以阴性对照为依据）：

- **Engagelab（WhatsApp）** ``GET https://wa.api.engagelab.cc/v1/templates``
  + ``Authorization: Basic base64(apiKey:apiSecret)``
  → 200 返回模板数组为有效；乱写 → 401 ``{"code":2001,"message":"invalid token"}``。
- **Infobip** ``GET https://api.infobip.com/sms/1/logs?limit=1`` + ``Authorization: App {apiKey}``
  → 200 ``{"results":[]}`` 为有效；**401 = 失效**；403 ``"Unauthorized access"`` 只说明**该入口**
  没有这个 Key 的访问权限，「被服务端识别」不等于「可用」，故报 ``error``。
- **CM / cmtelecom** ``GET https://api.cm.com/v1.2/transactions/`` + ``X-CM-PRODUCTTOKEN: {productToken}``
  → 200 ``{"status":1,…}`` 有效；乱写 → 401 ``{"status":9,"message":"Authorization failed"}``。
  发送端点 ``gw.cmtelecom.com`` 报 ``No account found`` 是端点自身的报错，不代表凭据失效。
- **nxcloud** ``POST https://api.nxcloud.com/api/common/getBalance``（form: ``appkey`` + ``secretkey``）
  → 判别力在**字段值**：乱写凭据同样回 ``code:"0"``，但 ``balance``/``currency`` 全为 ``null``。
- **laaffic** ``GET https://api.laaffic.com/v3/getBalance``
  + ``Api-Key`` / ``Sign = MD5(apiKey+apiSecret+timestamp)`` / ``Timestamp``
  → 鉴权失败 ``{"status":"-1","reason":"Authentication error"}``；
  ``{"status":"-2","reason":"Restricted IP access"}`` 说明凭据正确但源 IP 不在白名单，仍判有效。
- **buka / itniotech** ``GET https://api.itniotech.com/sms/getBalance``（同样的三个头）
  → 200 ``{"status":"0","balance":"0","reason":"success"}`` 有效；
  乱写 → ``{"status":"-1","reason":"Authentication error"}``。
- **Ultramsg** 无法校验：``api.ultramsg.com`` 对**任意**路径都返回
  404 ``{"error":"Your instance has been Stopped due to non-payment…"}``，假 token、假 instanceId
  同一段文案，无可区分信号 ⇒ 报 ``unknown``。

仅从框架层 backend.plugin_sdk 复用通用工具。

参考文档（Engagelab）：https://www.engagelab.com/zh_CN/docs/whatsapp/rest-api/template-management-api
参考文档（Infobip）：https://www.infobip.com/docs/api
参考文档（CM / cmtelecom）：https://developers.cm.com/
参考文档（laaffic）：https://www.laaffic.com/api/account/getBalance/
参考文档（buka / itniotech）：https://www.itniotech.com/api/account/getBalance/
参考文档（Ultramsg）：https://docs.ultramsg.com/
"""
from __future__ import annotations

import base64
import hashlib
import time
from datetime import datetime, timedelta, timezone

from backend.plugin_sdk import VerifyResult, body_snippet, first_list, guarded, log, make_client

NAME = "短信服务商"
VENDOR_FIELD = "vendor"
KEY_FIELD = "app_key"
SECRET_FIELD = "app_secret"
EXTRA_FIELD = "extra_params"

# 有只读接口、可自动判定的 8 家
VENDOR_ENGAGELAB = "engagelab"
VENDOR_INFOBIP_IN = "infobip_in"
VENDOR_INFOBIP_HK = "infobip_hk"
VENDOR_CM = "cm"
VENDOR_LAAFFIC = "laaffic"
VENDOR_NXCLOUD = "nxcloud"
VENDOR_BUKA = "buka"
VENDOR_ULTRAMSG = "ultramsg"

NETWORK_VENDORS = frozenset({
    VENDOR_ENGAGELAB, VENDOR_INFOBIP_IN, VENDOR_INFOBIP_HK, VENDOR_CM,
    VENDOR_LAAFFIC, VENDOR_NXCLOUD, VENDOR_BUKA, VENDOR_ULTRAMSG,
})
# 需要「标识 + 密钥」成对的服务商（其余三类只需标识）
PAIRED_VENDORS = frozenset({VENDOR_ENGAGELAB, VENDOR_LAAFFIC, VENDOR_NXCLOUD, VENDOR_BUKA, VENDOR_ULTRAMSG})

# 仅用于拼提示语；键与 manifest 的 select options 一一对应
VENDOR_LABELS = {
    "kmi": "kmi",
    "nxcloud": "nxcloud",
    "infobip_in": "Infobip（印度）",
    "infobip_hk": "Infobip（香港 / info-bip-hk）",
    "infinity": "infinity",
    "zhuwei": "zhuwei / zw",
    "cm": "CM / cmtelecom",
    "hkmi": "hkmi",
    "buka": "buka / itniotech",
    "iway": "iway",
    "laaffic": "laaffic",
    "mengwang": "mengwang",
    "linkinfo": "linkinfo",
    "eva": "eva",
    "engagelab": "WhatsApp Engagelab",
    "ultramsg": "Ultramsg",
}

ENGAGELAB_TEMPLATES_URL = "https://wa.api.engagelab.cc/v1/templates"
INFOBIP_LOGS_URL = "https://api.infobip.com/sms/1/logs"
CM_TRANSACTIONS_URL = "https://api.cm.com/v1.2/transactions/"
LAAFFIC_BALANCE_URL = "https://api.laaffic.com/v3/getBalance"
NXCLOUD_BALANCE_URL = "https://api.nxcloud.com/api/common/getBalance"
BUKA_BALANCE_URL = "https://api.itniotech.com/sms/getBalance"
ULTRAMSG_STATUS_URL = "https://api.ultramsg.com/{instance}/instance/status"

# 签名类的两个头：签名口径 MD5(标识 + 密钥 + 时间戳)，时间戳单位为**秒**
# （毫秒会拿到 {"status":"-16","reason":"Timestamp expires"}）
SIGN_TIMESTAMP_MS = False

# Engagelab：HTTP 200 但 errcode 1002 表示它没认出 app_key（请求头写法问题，不是凭据问题）
ENGAGELAB_ERRCODE_APP_KEY_MISSING = 1002
# Infobip：403 只有在带这段文案时才算「凭据被认可但无权访问」
INFOBIP_UNAUTHORIZED_MARK = "unauthorized"
# laaffic：鉴权已过、但源 IP 不在白名单（凭据本身是对的）
LAAFFIC_IP_RESTRICTED_STATUS = "-2"
# 签名类统一的「鉴权失败」特征
AUTH_FAILED_MARK = "authentication error"
# 签名类的前置校验特征：缺某个头时回 ``The X cannot be empty``，属请求没拼全，与凭据无关
MISSING_FIELD_MARK = "cannot be empty"
# Ultramsg：这段欠费文案对任意 instance/token 都返回，据此识别「拿不到有效信号」
ULTRAMSG_BLANKET_MARK = "non-payment"


# ── 小工具 ──────────────────────────────────────────────

def json_any(response):
    """解析 JSON，**不限 dict**。

    backend.plugin_sdk 的 ``safe_json`` 只认 dict，而 Engagelab 的模板列表顶层就是个 JSON 数组，
    所以这里自带一个；解析不出来返回 None。
    """
    try:
        return response.json()
    except Exception:
        return None


def _status_text(payload) -> str:
    """把回包里的 status 字段统一成字符串（有的家是 ``1``，有的是 ``"-1"``）。"""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("status")
    return "" if value is None else str(value)


def sign_timestamp() -> str:
    """签名用的时间戳（单位由 ``SIGN_TIMESTAMP_MS`` 决定）。"""
    now = time.time()
    return str(int(now * 1000) if SIGN_TIMESTAMP_MS else int(now))


def sign_headers(key: str, secret: str) -> dict:
    """laaffic / buka 共用的签名头：``Sign = MD5(标识 + 密钥 + 时间戳)``。"""
    stamp = sign_timestamp()
    return {
        "Api-Key": key,
        "Sign": hashlib.md5(f"{key}{secret}{stamp}".encode()).hexdigest(),
        "Timestamp": stamp,
        "Content-Type": "application/json",  # 缺此头会被直接拒掉，判定无从谈起
    }


def request_shape_error(text: str) -> bool:
    """``The X cannot be empty``：前置校验拦下，说明请求头没拼全，**不是**凭据问题。

    签名类接口的 ``status:"-1"`` 有两种语义——``Authentication error`` 才是凭据无效；
    缺头那个必须报 error 而不是 invalid，否则会把好凭据判成失效。
    """
    return MISSING_FIELD_MARK in (text or "").lower()


# ── Engagelab（WhatsApp）────────────────────────────────

def _verify_engagelab(api_key: str, api_secret: str) -> VerifyResult:
    if not (api_key and api_secret):
        return VerifyResult(
            status="error",
            message="Engagelab 需要 apiKey 与 apiSecret 成对提供（apiKey 填「标识」、apiSecret 填「密钥」）",
        )
    basic = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    with make_client() as client:
        response = client.get(
            ENGAGELAB_TEMPLATES_URL,
            headers={"Authorization": f"Basic {basic}"},
            params={"page": 1, "page_size": 1},
        )
    payload = json_any(response)
    if isinstance(payload, dict) and payload.get("errcode") == ENGAGELAB_ERRCODE_APP_KEY_MISSING:
        return VerifyResult(
            status="error",
            message="Engagelab 未识别 Basic 认证头（errcode 1002），凭据是否有效无法判断",
        )
    if response.status_code == 200:
        templates = payload if isinstance(payload, list) else first_list(payload, "items", "data", "templates")
        return VerifyResult(
            status="valid",
            message=f"Engagelab 凭据有效（模板接口可读，返回 {len(templates)} 条）",
            account_info={"vendor": VENDOR_ENGAGELAB, "templates": len(templates)},
        )
    detail = body_snippet(response)
    if response.status_code == 401:
        return VerifyResult(
            status="invalid",
            message=f"Engagelab 凭据无效（HTTP 401 {detail}）",
            account_info={"vendor": VENDOR_ENGAGELAB},
        )
    if response.status_code == 429:
        return VerifyResult(status="error", message="Engagelab 请求过于频繁，稍后重试（与凭据无关）")
    return VerifyResult(
        status="error",
        message=f"Engagelab 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── Infobip ─────────────────────────────────────────────

def _verify_infobip(api_key: str, vendor: str) -> VerifyResult:
    label = VENDOR_LABELS.get(vendor, "Infobip")
    if not api_key:
        return VerifyResult(status="error", message=f"{label} 需要 apiKey（填「标识」栏）")
    with make_client() as client:
        response = client.get(
            INFOBIP_LOGS_URL,
            headers={"Authorization": f"App {api_key}"},
            params={"limit": 1},
        )
    detail = body_snippet(response)
    if response.status_code == 200:
        payload = json_any(response)
        rows = payload.get("results") if isinstance(payload, dict) else None
        tail = f"短信日志接口可读，{len(rows)} 条记录" if isinstance(rows, list) else "接口可读"
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（{tail}）",
            account_info={"vendor": vendor},
        )
    if response.status_code == 401:
        return VerifyResult(
            status="invalid",
            message=f"{label} 凭据无效（HTTP 401 {detail}）",
            account_info={"vendor": vendor},
        )
    if response.status_code == 403 and INFOBIP_UNAUTHORIZED_MARK in detail.lower():
        # 该入口对这个 key 没有访问权限（对照：凭据无效是 401 "Invalid login details"）。
        # 403 只能说明 key 被服务端「识别」，不能说明它「能用」——如实报 error，不判有效。
        return VerifyResult(
            status="error",
            message=f"{label} 当前入口无此 Key 的访问权限（HTTP 403 {detail}），凭据真伪未经证实；"
                    f"该 key 与另一把不是同一登记，须在它自己的实例域名下校验",
            account_info={"vendor": vendor},
        )
    if response.status_code == 429:
        return VerifyResult(status="error", message=f"{label} 请求过于频繁，稍后重试（与凭据无关）")
    return VerifyResult(
        status="error",
        message=f"{label} 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── CM / cmtelecom ──────────────────────────────────────

def _cm_window() -> dict:
    """交易查询的时间窗（近 7 天，RFC3339 UTC）。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    return {
        "startdate": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enddate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _verify_cm(product_token: str) -> VerifyResult:
    label = VENDOR_LABELS[VENDOR_CM]
    if not product_token:
        return VerifyResult(status="error", message=f"{label} 需要 productToken（填「标识」栏）")
    with make_client() as client:
        response = client.get(
            CM_TRANSACTIONS_URL,
            headers={"X-CM-PRODUCTTOKEN": product_token},
            params=_cm_window(),
        )
    payload = json_any(response)
    detail = body_snippet(response)
    if response.status_code == 200 and _status_text(payload) == "1":
        message = payload.get("message") if isinstance(payload, dict) else ""
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（交易接口可读：{message or 'HTTP 200'}）",
            account_info={"vendor": VENDOR_CM},
        )
    if response.status_code == 401 or _status_text(payload) == "9":
        return VerifyResult(
            status="invalid",
            message=f"{label} 凭据无效（HTTP {response.status_code} {detail}）",
            account_info={"vendor": VENDOR_CM},
        )
    return VerifyResult(
        status="error",
        message=f"{label} 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── laaffic ─────────────────────────────────────────────

def _verify_laaffic(api_key: str, api_secret: str) -> VerifyResult:
    label = VENDOR_LABELS[VENDOR_LAAFFIC]
    if not (api_key and api_secret):
        return VerifyResult(status="error", message=f"{label} 需要 apiKey 与 secretKey 成对提供")
    with make_client() as client:
        response = client.get(LAAFFIC_BALANCE_URL, headers=sign_headers(api_key, api_secret))
    payload = json_any(response)
    reason = str(payload.get("reason") or "") if isinstance(payload, dict) else ""
    detail = body_snippet(response)
    if _status_text(payload) == LAAFFIC_IP_RESTRICTED_STATUS:
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（鉴权已通过，随后被源 IP 白名单拦下：{reason or 'Restricted IP access'}）"
                    f"，须从白名单内的源 IP 使用",
            account_info={"vendor": VENDOR_LAAFFIC},
        )
    if request_shape_error(reason) or request_shape_error(detail):
        return VerifyResult(
            status="error",
            message=f"{label} 请求头未拼全（{reason or detail}），与凭据无关，属插件实现问题",
        )
    if AUTH_FAILED_MARK in reason.lower() or AUTH_FAILED_MARK in detail.lower():
        return VerifyResult(
            status="invalid",
            message=f"{label} 凭据无效（{reason or detail}）",
            account_info={"vendor": VENDOR_LAAFFIC},
        )
    if response.status_code == 200 and _status_text(payload) in ("0", "1"):
        balance = payload.get("balance") if isinstance(payload, dict) else None
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（余额接口可读{'' if balance is None else f'：{balance}'}）",
            account_info={"vendor": VENDOR_LAAFFIC, "balance": balance},
        )
    return VerifyResult(
        status="error",
        message=f"{label} 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── nxcloud ─────────────────────────────────────────────

def _verify_nxcloud(app_key: str, secret_key: str) -> VerifyResult:
    label = VENDOR_LABELS[VENDOR_NXCLOUD]
    if not (app_key and secret_key):
        return VerifyResult(status="error", message=f"{label} 需要 appKey 与 secretKey 成对提供")
    with make_client() as client:
        response = client.post(NXCLOUD_BALANCE_URL, data={"appkey": app_key, "secretkey": secret_key})
    payload = json_any(response)
    detail = body_snippet(response)
    if isinstance(payload, dict) and str(payload.get("code")) == "0":
        # 关键：乱写凭据同样回 code 0，判别力在 balance/currency 是否为 null
        balance = payload.get("balance")
        currency = str(payload.get("currency") or "").strip()
        if balance is None and not currency:
            return VerifyResult(
                status="invalid",
                message=f"{label} 凭据无效（返回 code 0 但 balance/currency 均为空——该家对无效凭据也回 code 0）",
                account_info={"vendor": VENDOR_NXCLOUD},
            )
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（余额 {balance} {currency}）",
            account_info={"vendor": VENDOR_NXCLOUD, "balance": balance, "currency": currency or None},
        )
    return VerifyResult(
        status="error",
        message=f"{label} 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── buka / itniotech ────────────────────────────────────

def _verify_buka(api_key: str, api_secret: str) -> VerifyResult:
    label = VENDOR_LABELS[VENDOR_BUKA]
    if not (api_key and api_secret):
        return VerifyResult(status="error", message=f"{label} 需要 apiKey 与 apiSecret 成对提供")
    with make_client() as client:
        response = client.get(BUKA_BALANCE_URL, headers=sign_headers(api_key, api_secret))
    payload = json_any(response)
    detail = body_snippet(response)
    reason = str(payload.get("reason") or "") if isinstance(payload, dict) else ""
    if _status_text(payload) == "0":
        balance = payload.get("balance") if isinstance(payload, dict) else None
        return VerifyResult(
            status="valid",
            message=f"{label} 凭据有效（余额 {balance}）",
            account_info={"vendor": VENDOR_BUKA, "balance": balance},
        )
    # status "-1" 有两种语义，必须看 reason：缺头 = 请求没拼全（error），不是凭据失效
    if request_shape_error(reason) or request_shape_error(detail):
        return VerifyResult(
            status="error",
            message=f"{label} 请求头未拼全（{reason or detail}），与凭据无关，属插件实现问题",
        )
    if AUTH_FAILED_MARK in reason.lower() or AUTH_FAILED_MARK in detail.lower():
        return VerifyResult(
            status="invalid",
            message=f"{label} 凭据无效（{reason or detail}）",
            account_info={"vendor": VENDOR_BUKA},
        )
    return VerifyResult(
        status="error",
        message=f"{label} 返回 HTTP {response.status_code}，无法判定凭据：{detail}",
    )


# ── Ultramsg ────────────────────────────────────────────

def _ultramsg_instance(raw: str) -> str:
    """Ultramsg 的实例号在 URL 里写作 ``instance12345``；只填数字时补上前缀。"""
    value = raw.strip()
    return f"instance{value}" if value.isdigit() else value


def _verify_ultramsg(instance: str, token: str) -> VerifyResult:
    if not (instance and token):
        return VerifyResult(
            status="error",
            message="Ultramsg 需要 instanceId（填「标识」）与 token（填「密钥」）",
        )
    with make_client() as client:
        response = client.get(
            ULTRAMSG_STATUS_URL.format(instance=_ultramsg_instance(instance)),
            params={"token": token},
        )
    detail = body_snippet(response)
    if response.status_code in (401, 403):
        return VerifyResult(status="invalid", message=f"Ultramsg 凭据无效（HTTP {response.status_code} {detail}）")
    if response.status_code == 200 and ULTRAMSG_BLANKET_MARK not in detail.lower():
        return VerifyResult(status="valid", message="Ultramsg 凭据有效（实例状态接口可读）")
    # 该域名对任意路径/实例/口令都回同一段「实例因欠费停止」文案，区分不出真伪
    return VerifyResult(
        status="unknown",
        message="Ultramsg 未作判定：该域名对任意路径与任意 instance/token 都返回同一段「实例因欠费停止」"
                f"文案（HTTP {response.status_code}），凭据真伪无法区分。请在 Ultramsg 后台确认实例状态。",
    )


# ── 其余 8 家：如实报「未验证」──────────────────────────

def _unverifiable(vendor: str) -> VerifyResult:
    label = VENDOR_LABELS.get(vendor)
    if not label:
        return VerifyResult(
            status="unknown",
            message="未指定服务商，无法选择只读校验接口，未做联网校验",
        )
    return VerifyResult(
        status="unknown",
        message=f"{label}：未找到可用的只读校验接口，未做联网校验（其发送接口把参数校验放在鉴权之前，"
                f"探活只能得到「参数错误」；真发短信又有费用与风控副作用），故不下结论。",
        account_info={"vendor": vendor},
    )


# ── 插件接口 ────────────────────────────────────────────

def validate(data) -> list[str]:
    """校验配置，返回错误列表（空列表表示通过）。"""
    if not data:
        return ["配置数据为空"]
    vendor = str(data.get(VENDOR_FIELD) or "").strip()
    key = str(data.get(KEY_FIELD) or "").strip()
    secret = str(data.get(SECRET_FIELD) or "").strip()
    extra = str(data.get(EXTRA_FIELD) or "").strip()
    errors: list[str] = []
    if not vendor:
        errors.append("未选择服务商")
    label = VENDOR_LABELS.get(vendor, vendor)
    if vendor in PAIRED_VENDORS:
        if not (key and secret):
            errors.append(f"{label} 需要「标识」「密钥」成对填写")
    elif vendor in NETWORK_VENDORS:
        if not key:
            errors.append(f"{label} 需要填「标识」栏")
    elif not (key or secret or extra):
        errors.append("「标识」「密钥」「附加参数」至少要填一项")
    if extra:
        pieces = extra.replace("\n", ";").split(";")
        if any(piece.strip() and "=" not in piece for piece in pieces):
            errors.append("附加参数每段都应写成 name=value（用 ; 或换行分隔）")
    return errors


@guarded
def verify(data) -> VerifyResult:
    data = data or {}
    vendor = str(data.get(VENDOR_FIELD) or "").strip()
    key = str(data.get(KEY_FIELD) or "").strip()
    secret = str(data.get(SECRET_FIELD) or "").strip()
    extra = str(data.get(EXTRA_FIELD) or "").strip()

    if not (key or secret or extra):
        return VerifyResult(status="invalid", message="缺少必填字段：标识 / 密钥")

    log(f"校验短信服务商 {VENDOR_LABELS.get(vendor, vendor or '(未指定)')}（只读探针，不发短信）")
    if vendor == VENDOR_ENGAGELAB:
        return _verify_engagelab(key, secret)
    if vendor in (VENDOR_INFOBIP_IN, VENDOR_INFOBIP_HK):
        return _verify_infobip(key, vendor)
    if vendor == VENDOR_CM:
        return _verify_cm(key)
    if vendor == VENDOR_LAAFFIC:
        return _verify_laaffic(key, secret)
    if vendor == VENDOR_NXCLOUD:
        return _verify_nxcloud(key, secret)
    if vendor == VENDOR_BUKA:
        return _verify_buka(key, secret)
    if vendor == VENDOR_ULTRAMSG:
        return _verify_ultramsg(key, secret)
    return _unverifiable(vendor)


def get_display_summary(data) -> str:
    """列表展示用服务商名；标识与密钥绝不回显。"""
    vendor = str((data or {}).get(VENDOR_FIELD) or "").strip()
    label = VENDOR_LABELS.get(vendor)
    return f"{NAME} | {label}" if label else f"{NAME} | 未指定"
