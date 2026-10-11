"""天翼云 (CTYun) AK/SK 插件（plugins/cloud/ 类别下的独立子插件）。

天翼云走的是自研的 **EOP 网关**（EOP = 天翼云统一开放平台），签名算法与阿里云 / 腾讯云 /
华为云都不同源，与 AWS SigV4 也没有亲缘关系：

- 待签串只签两个强制头（``ctyun-eop-request-id`` / ``eop-date``）＋ query ＋ body 哈希，
  header 段与 query 段之间是**一个空行**（两个 ``\\n``）；
- 密钥派生链是 ``HMAC(sk, eop_date) → HMAC(·, ak) → HMAC(·, yyyymmdd)``，签名编码是 **base64**；
- 授权头**没有算法前缀词**，形如 ``{ak} Headers=ctyun-eop-request-id;eop-date Signature=…``；
- ``eop-date`` 取**北京时区**（格式里的尾部 ``Z`` 是字面量，不是 UTC 标记）；
- **成功判定不是 HTTP 200**，而是 body 里 ``statusCode == 800``（官方 Go SDK 的
  ``CtyunResponseModel.IsSuccess()``）。只看到 200 不能算成功。

签名实现全部在同目录的 ``eop_signer.py``（由官方 Go SDK 源码逐字校准，附 4 组黄金向量与
``interpret()`` 自测），本文件只负责「取签名 → 用 ``make_client()`` 发请求 → 判读结果」。

**本插件刻意只覆盖一个接口，这不是遗漏。** 天翼云官方**没有 Python SDK**（PyPI 上的
``ctyun`` 是无关的库存管理包），能对标的只有官方 Go SDK 源码与文档；实测仅确认了
``GET /v4/region/list-regions``（用无效 AK 打通到 EOP 网关，得到 401 +
``eopErrCode=10002``，说明端点、请求形态与签名流程都正确）。其余接口——包括
``/v4/region/get-zones``、``/v4/ecs/availability-zones/details``、ECS 实例列表等——
**在没有真实凭据跑通一次成功响应之前一律不加**：签名对不对可以靠源码校准，但响应体的
字段层次只能靠实测，猜错的结果是一张永远为空的表。宁可少而准。

本插件提供的操作（``actions``）：

- ``list_regions``  地域（资源池）列表 —— 该账号可见的资源池 ID / 名称 / 所属省份 /
  类型 / 可用区。凭据里填了「资源池 ID」时只显示匹配的那一条。

判定口径（与框架其它插件一致）：只有**明确**指向「AK 不存在（10002）/ 签名校验失败
（10003）」才判 ``invalid``；账号被禁用、服务未开通、IP 白名单、配额用尽这类
「密钥本身没问题、只是用不了」的情况判 ``restricted``（有效但受限）。

参考文档：https://www.ctyun.cn/document/10026730/10040588
参考文档：https://www.esurfingcloud.com/document/zh-cn/10027004/10028368
参考文档：https://www.esurfingcloud.com/document/zh-cn/10027004/10028371
"""
from __future__ import annotations

import json
import os
import sys

from backend.plugin_sdk import (
    VerifyResult,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "天翼云"
DISPLAY_FIELD = "access_key"

REQUIRED = (("access_key", "AccessKey (AK)"), ("secret_key", "SecurityKey (SK)"))

#: 天翼云没有统一的全域域名，各产线各自一个；list-regions 属 ECS/资源池产线（ctecs），
#: 该产线**有** global 汇聚域名（官方 Go SDK ``ctecs/const.go``: UrlProdCtecs）。
CTYUN_ENDPOINT = "ctecs-global.ctapi.ctyun.cn"
LIST_REGIONS_PATH = "/v4/region/list-regions"

#: 探针被拒时**明确指向密钥本身**的 EOP 鉴权错误码：
#:   10002 → AK 在 EOP 系统中不存在；10003 → 签名校验失败（SK 错 / 算法不符）。
_INVALID_EOP_CODES = {"10002", "10003"}

#: 同上，但用实测回包里的原话兜底（网关有时只把码塞在 message 里）
_INVALID_HINTS = ("invalid app id", "cannot find consumer", "signature verify", "签名校验")

#: ``signer.interpret()`` 的三态 → 框架的判定口径。**必须显式重映射**：
#: interpret 说的是 ``ok`` / ``bad_cred`` / ``error``，框架的 ``status`` 认的是
#: ``valid`` / ``invalid`` / ``restricted`` / ``error``，字面量一个都不能直接漏过去。
_VERDICT_STATUS = {"ok": "valid", "bad_cred": "invalid", "error": "error"}


# ── 加载同目录的签名模块 ──────────────────────────────────
# 文件名带厂商前缀，刻意**不用**通用的 ``signer.py``：``plugins/cloud/`` 是共享的类别目录、
# 也挂在 ``sys.path`` 上，几个云插件各带一份同名模块时，``sys.modules`` 里先加载的那份会
# 一直霸占该名字，后加载的插件 ``import signer`` 会拿到**别人的实现**（算法不同也不报错，
# 只会算出看不懂的错误码）。唯一命名后这类冲突从根上消失。
#
# 仍按**显式文件路径**加载而非裸 ``import``：框架加载插件时会临时把插件目录插进 ``sys.path``，
# 但 ``tests/test_real_vendors.py`` 这类直接 exec 插件文件的调用方只注入到**类别目录**，
# 裸 import 在那里会失败。
def _load_signer():
    """加载同目录的 ``eop_signer.py``（天翼云 EOP 签名实现）。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eop_signer.py")
    spec = importlib.util.spec_from_file_location("_vault_ctyun_signer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载天翼云签名模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vault_ctyun_signer"] = module
    spec.loader.exec_module(module)
    return module
signer = _load_signer()


# ── 基础工具 ────────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在响应文本里的凭据替换掉，任何路径都不回显密钥。

    这不是多余的谨慎：EOP 网关的 401 回包**会把 AK 原样写在 message 里**
    （``cannot find consumer:<AK>``），而这句话会径直进入凭据的 ``status_detail``。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _parse_json_body(body_text: str):
    """把响应文本解析成 dict；不是 JSON / 顶层非 dict 时返回 None。

    单独写一个而不是直接用 ``plugin_sdk.safe_json``：那个要的是 httpx 响应对象，
    而 ``interpret()`` 这一路的入参是已经读出来的文本。
    """
    try:
        parsed = json.loads(body_text) if (body_text or "").strip() else None
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _probe(ak: str, sk: str):
    """发一次只读的 list-regions 请求，返回 httpx 响应。

    ``follow_redirects=False``：签名覆盖了 host 与路径，被重定向后签名必然对不上，
    与其拿到一个看不懂的 401，不如让 3xx 原样暴露出来。
    """
    method, url, headers, body = signer.sign_probe(ak, sk)
    with make_client(follow_redirects=False) as client:
        return client.request(method, url, headers=headers, content=body)


# ── 判定（interpret 之上再分一层）────────────────────────

def _credential_fault(status_code: int, body_text: str) -> bool:
    """该 401/403 是否**明确**指向 AK/签名本身（→ ``invalid``）。

    证据来源与 ``signer.interpret()`` 一致：EOP 鉴权错误码 10002 / 10003，以及实测回包里
    ``Invalid APP ID`` / ``cannot find consumer`` 这类原话。

    为什么要单独判一次：``interpret()`` 把**任何** 401/403 都归为 ``bad_cred``——对
    「AK 不存在 / 签名错」是对的，但会把「账号被禁用 / 服务未开通 / IP 白名单」这类
    **密钥本身没问题、只是用不了**的情况也判死。框架的口径要求前者 ``invalid``、
    后者 ``restricted``，所以这里补上这层区分。
    """
    if status_code not in (401, 403):
        return False
    payload = _parse_json_body(body_text)
    if isinstance(payload, dict):
        codes = set()
        for key in ("eopErrCode", "errorCode", "error", "code", "statusCode"):
            value = str(payload.get(key) or "").strip().upper().replace("_", "")
            # statusCode 形如 "CTAPI_10002"，剥掉厂商前缀后才是错误码
            codes.add(value)
            codes.add(value.replace("CTAPI", "").strip())
        if codes & _INVALID_EOP_CODES:
            return True
    blob = (body_text or "").lower()
    return any(hint in blob for hint in _INVALID_HINTS)


def _classify(status_code: int, body_text: str, ak: str, sk: str) -> tuple[str, str]:
    """把一次探针响应判成框架口径的 ``(status, message)``，消息已脱敏。

    ``status`` ∈ ``valid`` / ``invalid`` / ``restricted`` / ``error``。
    """
    verdict = signer.interpret(status_code, body_text)
    raw = str(verdict.get("verdict") or "")
    status = _VERDICT_STATUS.get(raw, "error")
    message = _redact(str(verdict.get("message") or ""), ak, sk)

    if raw not in _VERDICT_STATUS:
        # 签名器以后新增了 verdict 取值而这里没跟上时，宁可报 error，也**绝不**把原始
        # 字面量（如 ok / bad_cred）当成框架 status 漏出去 —— 那会让前端整行显示错色。
        message = f"未识别的判定结果（{raw or '空'}）：{message}"

    if raw == "bad_cred" and not _credential_fault(status_code, body_text):
        # 被拒但没有「AK 不存在 / 签名错」的明确证据 ⇒ 密钥可能有效，只是用不了。
        # 必须换掉 interpret 的措辞：它那句「AK/SK 无效或签名不匹配」是给 bad_cred 写的，
        # 挂在 restricted 上会自相矛盾，用户按「密钥坏了」去排查就找错方向了。
        return "restricted", f"凭据被网关拒绝，但未指向 AK/SK 本身（账号禁用 / 服务未开通 /" \
                             f"权限不足 / IP 白名单均有可能）：{message}"
    return status, message


# ── 响应解析 ────────────────────────────────────────────

def _region_items(payload) -> list | None:
    """从响应信封里取出 ``regionList``；信封里根本没有这个键时返回 ``None``。

    官方 Go SDK 的解析路径是 ``returnObj.regionList``（``RegionListApi.Do`` 把
    ``returnObj`` 解到 ``regionListRealResponse``）。为兼容网关可能把业务数据提到顶层
    的形态，这里先试 ``returnObj``、再试顶层；**两级都找不到返回 None**，让调用方如实
    报「响应里没有资源池列表」，而不是把「字段名不对」伪装成「这个账号没有资源池」。
    """
    if not isinstance(payload, dict):
        return None
    for node in (payload.get("returnObj"), payload):
        if isinstance(node, dict):
            items = node.get("regionList")
            if isinstance(items, list):
                return items
    return None


def _zone_list(item: dict) -> list[str]:
    raw = item.get("zoneList")
    if not isinstance(raw, list):
        return []
    return [str(zone).strip() for zone in raw if str(zone).strip()]


def _region_rows(items: list, wanted: str) -> list[dict]:
    """把 regionList 转成表格行；``wanted`` 非空时按资源池 ID 或名称精确匹配过滤。"""
    rows: list[dict] = []
    needle = wanted.lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        # 官方 SDK 的 JSON tag 是 regionID；顺手兼容下 camelCase 写法
        region_id = str(item.get("regionID") or item.get("regionId") or "").strip()
        region_name = str(item.get("regionName") or "").strip()
        if needle and needle not in (region_id.lower(), region_name.lower()):
            continue
        zones = _zone_list(item)
        rows.append({
            "RegionId": region_id,
            "RegionName": region_name,
            "RegionParent": str(item.get("regionParent") or "").strip(),
            "RegionType": str(item.get("regionType") or "").strip(),
            "MultiZones": "是" if item.get("isMultiZones") else "否",
            "ZoneCount": len(zones),
            "Zones": "、".join(zones),
        })
    return rows


def _empty_text(wanted: str) -> str:
    if wanted:
        return f"没有匹配「{wanted}」的资源池（按资源池 ID 或名称精确匹配）"
    return "该账号下没有可见的资源池"


_REGION_COLUMNS = [
    {"key": "RegionId", "label": "资源池 ID", "mono": True},
    {"key": "RegionName", "label": "资源池名称"},
    {"key": "RegionParent", "label": "所属省份", "width": 140},
    {"key": "RegionType", "label": "类型", "width": 110, "type": "tag"},
    {"key": "MultiZones", "label": "多可用区", "width": 100, "type": "tag"},
    {"key": "ZoneCount", "label": "可用区数", "width": 100, "type": "number"},
    {"key": "Zones", "label": "可用区"},
]


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """只读探针：``GET /v4/region/list-regions``（EOP 网关）。

    零副作用——该接口只返回租户可见的资源池清单，不签发任何凭证、不改动任何资源。
    成功条件以官方 Go SDK 为准：HTTP 2xx **且** body ``statusCode == 800``。
    """
    data = data or {}
    missing = [label for key, label in REQUIRED if not str(data.get(key) or "").strip()]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    ak = _field(data, "access_key")
    sk = _field(data, "secret_key")
    log(f"校验天翼云凭据 {mask_secret(ak)}（EOP {CTYUN_ENDPOINT}{LIST_REGIONS_PATH}）")

    resp = _probe(ak, sk)
    status, message = _classify(resp.status_code, resp.text, ak, sk)
    return VerifyResult(status=status, message=message)


# ── 权限探测（capabilities）─────────────────────────────

def capabilities(data: dict) -> dict:
    """返回当前 AK/SK **真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    本插件的探针与 ``verify`` 是同一个接口，所以结论只有三种：

    - 明确成功 → ``list_regions`` 可用；
    - 明确被拒（``invalid`` / ``restricted``）→ 隐藏该操作并说明原因；
    - 探针本身没跑通（网络异常 / 响应看不懂）→ **抛异常**，交给框架按「未知 → 全部可用」
      兜底。探测抖一下就把唯一一个操作藏掉，比不探测还糟。
    """
    data = data or {}
    ak = _field(data, "access_key")
    sk = _field(data, "secret_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 AccessKey 或 SecurityKey"}

    resp = _probe(ak, sk)
    status, _message = _classify(resp.status_code, resp.text, ak, sk)

    if status == "valid":
        return {"available": ["list_regions"], "note": "AK/SK 有效"}
    if status == "invalid":
        return {"available": [], "note": "AK/SK 无效或签名不匹配，任何操作都会失败"}
    if status == "restricted":
        # 探针明确被拒（不是「拿不准」），那就如实隐藏：这对 AK 当前确实用不了
        return {"available": [], "note": "凭据被拒绝或受限制（账号未开通 / 权限不足 / IP 白名单）"}
    raise RuntimeError("天翼云权限探测失败（网络异常或响应无法识别）")


# ── 操作 ────────────────────────────────────────────────

def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据不完整时返回空列表）。"""
    data = data or {}
    if not (_field(data, "access_key") and _field(data, "secret_key")):
        return []
    return [
        {
            "id": "list_regions",
            "name": "地域列表",
            "kind": "list",
            "group": "计算",
            "order": 10,
            "description": (
                "EOP /v4/region/list-regions（只读）：该账号可见的资源池 ID、名称、所属省份、"
                "类型与可用区。凭据里填了「资源池 ID」时只显示匹配的那一条"
            ),
            "columns": _REGION_COLUMNS,
            "empty_text": "该账号下没有可见的资源池",
        },
    ]


def _run_list_regions(ak: str, sk: str, wanted: str) -> dict:
    """资源池列表（只读）。"""
    resp = _probe(ak, sk)
    status, message = _classify(resp.status_code, resp.text, ak, sk)
    if status != "valid":
        return {"ok": False, "error": message}

    items = _region_items(safe_json(resp))
    if items is None:
        return {"ok": False, "error": "响应里没有资源池列表（regionList），无法解析"}

    rows = _region_rows(items, wanted)
    return {
        "ok": True,
        "rows": rows,
        "columns": _REGION_COLUMNS,
        "total": len(rows),
        "has_more": False,
        "empty_text": _empty_text(wanted),
    }


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。"""
    data = data or {}
    try:
        ak = _field(data, "access_key")
        sk = _field(data, "secret_key")
        if not ak or not sk:
            return {"ok": False, "error": "AccessKey 或 SecurityKey 为空"}

        if action_id == "list_regions":
            return _run_list_regions(ak, sk, _field(data, "region_id"))
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        # 只回异常类型名：httpx 的异常文本含完整请求 URL 与请求头，会把 AK 泄进日志与界面
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
