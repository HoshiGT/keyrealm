"""UCloud 公私钥插件（plugins/cloud/ 类别下的独立子插件）。

UCloud 只有**一套**签名：SHA1 取 **hex**（不是 base64）、参数按键名升序、
**键与值之间无任何分隔符**、串末尾直拼 PrivateKey、全程**不做 URL 转义**。
实现与证据链在同目录的 ``ucloud_signer.py``（由官方 Python SDK 的黄金向量逐条自证）。
本文件只做三件事：用 ``make_client()`` 把签名器产出的 ``(method, url, headers, body)``
发出去、把回包里的 ``RetCode`` 判读成凭据状态、把列表铺成表格。

**本插件的每一个操作都是只读的**：不创建 / 删除 / 开关机 / 绑定任何资源，
请求只落在「查账号、查地域、查项目、查云主机、查弹性 IP、查防火墙」这六个列表接口上。

关于 Action 名的坑：官方文档「地域和可用区列表」一页的页尾写着「可通过 ``ListRegions``
与 ``ListZones`` 接口查询」，但官方 Python SDK 的服务方法清单里**没有这两个 Action**，
真实可用的是 uaccount 服务的 ``GetRegion``；``ListRegions`` 会被网关按「Action 不存在」
（``RetCode=161``）拒掉。**该结论无法用假密钥实测复现** —— 网关把签名校验排在 Action 校验
之前，未注册的密钥一律先撞 ``RetCode=171``（``Signature VerifyAC Error``），根本走不到 161。
（可以用不带 ``Signature`` / 不带 ``Action`` 的请求做正对照：回的是 ``RetCode=170``
``Missing signature`` 与 ``RetCode=160`` ``Missing Action``，说明网关确实读懂了本插件的
请求信封。）所以本插件每个 Action 都取自官方 SDK ——
``services/*/client.py`` 里 ``self.invoke("XxxAction", …)`` 那行才是权威 Action 名，
不照文档页尾抄。

本插件提供的操作（``actions``）：

- ``whoami``          账号信息（GetUserInfo + 账户余额 GetBalance + 可见地域计数）
- ``list_regions``    地域列表（GetRegion；行内可直接下钻到该地域的云主机 / 弹性 IP / 防火墙）
- ``list_projects``   项目列表（GetProjectList）
- ``list_hosts``      云主机列表（DescribeUHostInstance；地域留空则并发枚举全部可见地域）
- ``list_eips``       弹性 IP 列表（DescribeEIP；地域留空同上）
- ``list_firewalls``  防火墙列表（DescribeFirewall；地域留空同上）

判定口径（框架统一规则，与 ``signer.interpret()`` 逐条对齐）：只有**明确**指向
「用户不存在（172）/ 签名错误（171）/ 缺少签名（170、104、109）」才判 ``invalid``；
配额用尽、IP 白名单、IAM 权限不足、可用区权限错误、安全锁这类「密钥本身没问题、
只是当前用不了」的情况判 ``restricted``（有效但受限）。

参考文档：https://docs.ucloud.cn/api/summary/signature
参考文档：https://docs.ucloud.cn/api/summary/get_region
参考文档：https://docs.ucloud.cn/api/summary/get_project_list
参考文档：https://docs.ucloud.cn/api/uaccount-api/get_user_info
参考文档：https://docs.ucloud.cn/api/uaccount-api/get_balance
参考文档：https://docs.ucloud.cn/api/uhost-api/describe_uhost_instance
参考文档：https://docs.ucloud.cn/api/unet-api/describe_eip
参考文档：https://docs.ucloud.cn/api/unet-api/describe_firewall
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from backend.plugin_sdk import (
    VerifyResult,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

NAME = "UCloud"
DISPLAY_FIELD = "public_key"

REQUIRED = (("public_key", "PublicKey"), ("private_key", "PrivateKey"))

#: 单页条数。DescribeUHostInstance 的 ``Limit`` 上限就是 100，三个资源接口统一按它取，
#: 换页由 ``Offset`` 负责（本插件不翻页：拿到的条数与上游 ``TotalCount`` 不等时把
#: ``has_more`` 置真，界面上如实显示「仅显示部分」，绝不假装已经拿全了）。
_PAGE_SIZE = 100

#: 「地域留空 → 枚举全部地域」时的并发度。list 类操作**没有进度条通道**（框架只给
#: form/chat 类操作开流式），所以几十次请求必须在插件内并发跑完、一次返回。
_SWEEP_WORKERS = 8

#: 资源操作里地域字段的默认值。UCloud 的公有云主控地域，先给个能立刻跑通的，
#: 用户清空它就变成「枚举全部可见地域」。
DEFAULT_REGION = "cn-bj2"

#: ``signer.interpret()`` 的取值本身就是框架口径（valid / invalid / restricted / error），
#: 直接透传。这份集合只用来兜住「签名器以后新增了取值、这里没跟上」的情况 ——
#: 那种时候宁可报 error，也**绝不能**把未识别的字面量当 status 漏给前端（会整行显示错色）。
_KNOWN_VERDICTS = {"valid", "invalid", "restricted", "error"}


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
    """加载同目录的 ``ucloud_signer.py``（UCloud 签名实现）。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ucloud_signer.py")
    spec = importlib.util.spec_from_file_location("_vault_ucloud_signer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 UCloud 签名模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vault_ucloud_signer"] = module
    spec.loader.exec_module(module)
    return module
signer = _load_signer()

#: 接入点与签名器同源，不在本文件再抄一份域名
API_ENDPOINT = signer.API_ENDPOINT


# ── 基础工具 ────────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    """安全取值：转字符串并去首尾空白。"""
    value = (data or {}).get(key)
    if value is None:
        return ""
    return str(value).strip()


def _text(value) -> str:
    """标量 → 展示文本；``None`` 与空串统一成空串（不给界面留 ``None``）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_int(value):
    """转 int；转不了返回 ``None``（``None`` 与 0 是两回事，调用方要能区分）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _yes_no(value) -> str:
    """UCloud 的布尔字段在 JSON 里可能是 ``true`` / ``"true"`` / ``1`` / ``0``，统一成是/否。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    text = _text(value).lower()
    if not text:
        return ""
    if text in ("true", "1", "yes"):
        return "是"
    if text in ("false", "0", "no"):
        return "否"
    return _text(value)


def _fmt_time(value) -> str:
    """UCloud 的时间字段是 **Unix 秒**（int），过期/未设置时给 ``0`` 或空串。

    直接显示秒数对人不友好，统一成本地时间文本；非数字（个别接口会给字符串）
    就原样返回，不猜。
    """
    text = _text(value)
    if not text:
        return ""
    seconds = _as_int(text)
    if seconds is None:
        return text
    if seconds <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except (OverflowError, OSError, ValueError):
        return text


def _redact(text: str, *secrets: str) -> str:
    """把可能出现在响应文本里的凭据替换掉，任何路径都不回显密钥。

    上游的 ``Message`` 字段偶尔会把 ``PublicKey`` 原样写回来（例如用户不存在时），
    而这句话会径直进入凭据的 ``status_detail``。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _as_list(payload, key: str):
    """取信封里的列表字段；**不是列表时返回 ``None``**（区别于「是空列表」）。

    这个区分是刻意的：``None`` 表示字段名或层次猜错了（要如实报「无法解析」），
    ``[]`` 表示确实没有资源（该显示 ``empty_text``）。把前者当后者，等于把
    「插件解析错了」伪装成「你账号里没有资源」，是最难排查的一类空表。
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return None


def _first_item(payload, key: str):
    """取信封里某字段的第一个元素；字段缺失 / 空列表 → ``None``。

    兼容「上游把单个对象直接给出」的形态（个别接口的 ``DataSet`` 只放一个元素，
    有的版本则直接给对象），两种都能取到。
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else None
    return value if isinstance(value, dict) else None


# ── 请求构造与发送 ──────────────────────────────────────

def _build_request(action: str, public_key: str, private_key: str, extra: dict | None = None):
    """构造一个已签名的 UCloud 请求，返回 ``(method, url, headers, body)``。

    签名器的 ``sign_probe()`` 只签 ``{Action, PublicKey}``（探针不需要业务参数），
    所以带参数的操作在这里自己组装参数表。**但请求头一律复用 ``sign_probe()`` 的产出**，
    不另写一份 —— 头的构成（``Content-Type`` / ``User-Agent`` / ``U-Timestamp-Ms``）
    属于签名器的事实范围，两处各写一份迟早会漂移，而漂移的表现只是一个没头没脑的 400。
    """
    _, _, headers, _ = signer.sign_probe(public_key, private_key, action=action)

    params: dict = {"Action": action, "PublicKey": public_key}
    for key, value in (extra or {}).items():
        # 空值与 None 一律不下发：UCloud 对「传了空串」和「没传」的处理并不一致
        if value is None or str(value).strip() == "":
            continue
        params[key] = value

    params["Signature"] = signer.sign_params(private_key, params)
    body = json.dumps(
        OrderedDict(sorted(params.items(), key=lambda item: item[0])),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "POST", API_ENDPOINT, headers, body


def _request(client, public_key: str, private_key: str, action: str, extra: dict | None = None):
    """发一次已签名的 UCloud 调用，返回 ``(payload, error, verdict)``。

    成功时 ``payload`` 是完整响应体、``error`` 为空、``verdict`` 为 ``valid``；
    被拒或解析失败时 ``payload`` 为 None，``error`` 可直接展示。
    """
    method, url, headers, body = _build_request(action, public_key, private_key, extra)
    resp = client.request(method, url, headers=headers, content=body)

    payload = safe_json(resp)
    verdict = signer.interpret(resp.status_code, resp.text)
    raw = str(verdict.get("verdict") or "")
    if raw != "valid" or not isinstance(payload, dict):
        message = _redact(str(verdict.get("message") or ""), public_key, private_key)
        if not isinstance(payload, dict):
            message = f"响应无法解析（HTTP {resp.status_code}）"
        return None, message, raw
    return payload, "", raw


def _soft(client, public_key: str, private_key: str, action: str, extra: dict | None = None):
    """尽力而为地取一份数据：失败返回 ``(None, 原因)``，**绝不打断整条操作**。

    余额（``GetBalance``）对子账号常常是无权限的（IAM 校验不通过），但那不代表凭据无效，
    更不该让整块「账号信息」查不出来。
    """
    try:
        payload, error, _ = _request(client, public_key, private_key, action, extra)
    except Exception as exc:  # noqa: BLE001 - 尽力而为，绝不外抛
        # 只回异常类型名：httpx 的异常文本含完整请求 URL，会把密钥泄进界面
        return None, f"执行异常：{type(exc).__name__}"
    return (payload, "") if payload is not None else (None, error)


def _sweep(items, worker):
    """并发跑 ``worker(item)``，保持输入顺序，逐项吞异常。

    为什么自己起线程池而不是靠框架：list 类操作没有进度条通道（见 ``_SWEEP_WORKERS``），
    「地域留空 → 枚举全部地域」这种要发几十次请求的操作无法边跑边回报，必须在插件内
    并发做完、一次返回。

    ``client`` 由调用方在**自己的线程**里建好后传进 ``worker``，**绝不在 worker 里调
    ``make_client()``**：代理是 ``threading.local`` 的，工作线程里新建 client 会丢掉
    本次凭据配置的代理，静默走直连 —— 出口悄悄换掉，界面上完全看不出来。

    :return: ``[(item, result, "")]``；worker 抛异常的那一项是 ``(item, None, 异常类型名)``。
    """
    results = []
    if not items:
        return results
    with ThreadPoolExecutor(max_workers=min(_SWEEP_WORKERS, len(items))) as pool:
        futures = [(item, pool.submit(worker, item)) for item in items]
        for item, future in futures:
            try:
                results.append((item, future.result(), ""))
            except Exception as exc:  # noqa: BLE001 - 逐项兜底，不牵连同批其它项
                results.append((item, None, type(exc).__name__))
    return results


# ── 判定 ────────────────────────────────────────────────

def _classify(resp, public_key: str, private_key: str) -> tuple[str, str]:
    """把一次探针响应判成框架口径的 ``(status, message)``，消息已脱敏。

    判读**完全复用** ``signer.interpret()`` —— 它拿的就是 ``RetCode`` 分类表
    （``_INVALID_CODES`` / ``_RESTRICTED_CODES``，逐条对齐官方错误码说明）。
    这里只做两件事：脱敏，以及把未识别的 verdict 兜成 ``error``。
    """
    verdict = signer.interpret(resp.status_code, resp.text)
    raw = str(verdict.get("verdict") or "")
    message = _redact(str(verdict.get("message") or ""), public_key, private_key)
    if raw not in _KNOWN_VERDICTS:
        return "error", f"未识别的判定结果（{raw or '空'}）：{message}"
    return raw, message


# ── 列定义 ───────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]

_REGION_COLUMNS = [
    {"key": "Region", "label": "地域 ID", "mono": True, "width": 110},
    {"key": "RegionName", "label": "地域名称", "width": 130},
    {"key": "RegionId", "label": "地域编号", "width": 100, "type": "number"},
    {"key": "IsDefault", "label": "默认地域", "width": 100, "type": "tag"},
    {"key": "Zones", "label": "可用区"},
]

_PROJECT_COLUMNS = [
    {"key": "ProjectId", "label": "项目 ID", "mono": True},
    {"key": "ProjectName", "label": "项目名称"},
    {"key": "IsDefault", "label": "默认项目", "width": 100, "type": "tag"},
    {"key": "MemberCount", "label": "成员数", "width": 90, "type": "number"},
    {"key": "ResourceCount", "label": "资源数", "width": 90, "type": "number"},
    {"key": "ParentName", "label": "上级项目", "width": 130},
    {"key": "CreateTime", "label": "创建时间", "width": 170},
]

_HOST_COLUMNS = [
    {"key": "UHostId", "label": "主机 ID", "mono": True},
    {"key": "Name", "label": "名称"},
    {"key": "State", "label": "状态", "width": 110, "type": "tag"},
    {"key": "Region", "label": "地域", "width": 100, "mono": True},
    {"key": "Zone", "label": "可用区", "width": 110, "mono": True},
    {"key": "PublicIP", "label": "公网 IP", "mono": True, "width": 150},
    {"key": "InnerIP", "label": "内网 IP", "mono": True, "width": 150},
    {"key": "CPU", "label": "CPU", "width": 70, "type": "number"},
    {"key": "Memory", "label": "内存(MB)", "width": 100, "type": "number"},
    {"key": "UHostType", "label": "机型", "width": 90},
    {"key": "ChargeType", "label": "计费方式", "width": 110},
    {"key": "ExpireTime", "label": "到期时间", "width": 170},
]

_EIP_COLUMNS = [
    {"key": "EIPId", "label": "EIP ID", "mono": True},
    {"key": "Name", "label": "名称"},
    {"key": "IP", "label": "IP 地址", "mono": True, "width": 150},
    {"key": "Region", "label": "地域", "width": 100, "mono": True},
    {"key": "Status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "Bandwidth", "label": "带宽(Mbps)", "width": 110, "type": "number"},
    {"key": "PayMode", "label": "付费模式", "width": 100},
    {"key": "ChargeType", "label": "计费方式", "width": 110},
    {"key": "BoundTo", "label": "绑定资源"},
    {"key": "InnerIP", "label": "绑定内网 IP", "mono": True, "width": 150},
    {"key": "ExpireTime", "label": "到期时间", "width": 170},
]

_FIREWALL_COLUMNS = [
    {"key": "FWId", "label": "防火墙 ID", "mono": True},
    {"key": "Name", "label": "名称"},
    {"key": "Region", "label": "地域", "width": 100, "mono": True},
    {"key": "Type", "label": "类型", "width": 90, "type": "tag"},
    {"key": "RuleCount", "label": "规则数", "width": 90, "type": "number"},
    {"key": "ResourceCount", "label": "绑定资源数", "width": 110, "type": "number"},
    {"key": "Tag", "label": "标签", "width": 120},
    {"key": "CreateTime", "label": "创建时间", "width": 170},
]

#: 地域列表的行内下钻：三个资源操作都按 ``Region`` 收参，所以能一路带过去。
#: ``$row.<列key>`` 由前端解析，取不到值时按钮自动置灰（不会把空参数发出去）。
_REGION_ROW_ACTIONS = [
    {"id": "list_hosts", "label": "云主机", "params": {"region": "$row.Region"}},
    {"id": "list_eips", "label": "弹性 IP", "params": {"region": "$row.Region"}},
    {"id": "list_firewalls", "label": "防火墙", "params": {"region": "$row.Region"}},
]


def _list_result(rows, columns, empty_text: str, *, total=None, has_more: bool = False) -> dict:
    """统一的列表结果信封。

    ``total`` 默认取 ``len(rows)``；显式传入时表示「上游说总共这么多」，与 ``len(rows)``
    不等就说明被 ``Limit`` 截断了 —— 那正是 ``has_more`` 要置真的场合，
    界面上会显示「共 N 条（仅显示部分）」，而不是拿一页数据冒充全量。
    """
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows) if total is None else total,
        "has_more": has_more,
        "empty_text": empty_text,
    }


# ── 行构造 ──────────────────────────────────────────────

def _region_row(item: dict) -> dict:
    return {
        "Region": _text(item.get("Region")),
        "RegionName": _text(item.get("RegionName")),
        "RegionId": _text(item.get("RegionId")),
        "IsDefault": _yes_no(item.get("IsDefault")),
        # 官方 SDK 里 GetRegion 的 Zone 是**逗号分隔的字符串**（不是一个列表）
        "Zones": _text(item.get("Zone")),
    }


def _project_row(item: dict) -> dict:
    return {
        "ProjectId": _text(item.get("ProjectId")),
        "ProjectName": _text(item.get("ProjectName")),
        "IsDefault": _yes_no(item.get("IsDefault")),
        "MemberCount": _text(item.get("MemberCount")),
        "ResourceCount": _text(item.get("ResourceCount")),
        "ParentName": _text(item.get("ParentName")),
        "CreateTime": _fmt_time(item.get("CreateTime")),
    }


def _host_ips(item: dict) -> tuple[str, str]:
    """把 ``IPSet`` 拆成 ``(公网 IP, 内网 IP)``。

    ``IPSet[].Type`` 的取值是 ``Private`` / ``Bgp`` / ``Internation`` ——
    BGP 与境外线路都是公网，只有 ``Private`` 是内网。同类有多个就都列出来。
    """
    public: list[str] = []
    inner: list[str] = []
    for entry in item.get("IPSet") or []:
        if not isinstance(entry, dict):
            continue
        ip = _text(entry.get("IP"))
        if not ip:
            continue
        (inner if _text(entry.get("Type")).lower() == "private" else public).append(ip)
    return "、".join(public), "、".join(inner)


def _host_row(item: dict, region: str) -> dict:
    public_ip, inner_ip = _host_ips(item)
    return {
        "UHostId": _text(item.get("UHostId")),
        "Name": _text(item.get("Name")),
        "State": _text(item.get("State")),
        "Region": region,
        "Zone": _text(item.get("Zone")),
        "PublicIP": public_ip,
        "InnerIP": inner_ip,
        "CPU": _text(item.get("CPU")),
        "Memory": _text(item.get("Memory")),
        "UHostType": _text(item.get("UHostType")),
        "ChargeType": _text(item.get("ChargeType")),
        "ExpireTime": _fmt_time(item.get("ExpireTime")),
    }


def _eip_row(item: dict, region: str) -> dict:
    binding = item.get("EIPBinding") if isinstance(item.get("EIPBinding"), dict) else {}
    resource = item.get("Resource") if isinstance(item.get("Resource"), dict) else {}
    addresses = [
        _text(entry.get("IP"))
        for entry in item.get("EIPAddr") or []
        if isinstance(entry, dict) and _text(entry.get("IP"))
    ]
    # ResourceType 是产品名（uhost / ulb …），ResourceName 才是人看得懂的那个；
    # 新版本把同一组信息放在 Sub* 里，两个位置都试一遍。
    bound = "、".join(part for part in (
        _text(resource.get("ResourceType")) or _text(resource.get("SubResourceType")),
        _text(resource.get("ResourceName")) or _text(resource.get("SubResourceName")),
    ) if part)
    return {
        "EIPId": _text(item.get("EIPId")),
        "Name": _text(item.get("Name")),
        "IP": "、".join(addresses),
        "Region": region,
        "Status": _text(item.get("Status")),
        "Bandwidth": _text(item.get("Bandwidth")),
        "PayMode": _text(item.get("PayMode")),
        "ChargeType": _text(item.get("ChargeType")),
        "BoundTo": bound or "（未绑定）",
        "InnerIP": _text(binding.get("PrivateIP")),
        "ExpireTime": _fmt_time(item.get("ExpireTime")),
    }


def _firewall_row(item: dict, region: str) -> dict:
    rules = item.get("Rule") if isinstance(item.get("Rule"), list) else []
    return {
        "FWId": _text(item.get("FWId")),
        "Name": _text(item.get("Name")),
        "Region": region,
        "Type": _text(item.get("Type")),
        "RuleCount": len([rule for rule in rules if isinstance(rule, dict)]),
        "ResourceCount": _text(item.get("ResourceCount")),
        "Tag": _text(item.get("Tag")),
        "CreateTime": _fmt_time(item.get("CreateTime")),
    }


# ── 操作实现 ─────────────────────────────────────────────

def _visible_regions(client, public_key: str, private_key: str):
    """取该账号可见的地域 ID 列表（只读）。返回 ``(regions, error)``。"""
    payload, error, _ = _request(client, public_key, private_key, "GetRegion")
    if payload is None:
        return [], error
    items = _as_list(payload, "Regions")
    if items is None:
        return [], "响应里没有地域列表（Regions），无法解析"
    regions = [rid for rid in (_text(item.get("Region")) for item in items) if rid]
    return regions, ""


def _run_whoami(client, public_key: str, private_key: str) -> dict:
    """账号信息（只读）：GetUserInfo 为主体，余额与可见地域为尽力而为。"""
    payload, error, _ = _request(client, public_key, private_key, "GetUserInfo")
    if payload is None:
        return {"ok": False, "error": error}
    user = _first_item(payload, "DataSet")
    if user is None:
        return {"ok": False, "error": "响应里没有账号信息（DataSet），无法解析"}

    rows = [row for row in (
        {"item": "用户名", "value": _text(user.get("UserName"))},
        {"item": "邮箱", "value": _text(user.get("UserEmail"))},
        {"item": "手机号", "value": _text(user.get("UserPhone"))},
        {"item": "公司名称", "value": _text(user.get("CompanyName"))},
        {"item": "是否管理员", "value": _yes_no(user.get("Admin"))},
        {"item": "实名认证", "value": _yes_no(user.get("AuthState"))},
        {"item": "所在地", "value": " ".join(part for part in (
            _text(user.get("Province")), _text(user.get("City"))) if part)},
    ) if row["value"]]

    balance, balance_error = _soft(client, public_key, private_key, "GetBalance")
    if balance is not None:
        info = balance.get("AccountInfo") if isinstance(balance.get("AccountInfo"), dict) else {}
        # 官方 SDK 里这几个都是**字符串**（含小数），原样展示就是元
        for label, key in (("账户余额(元)", "Amount"), ("可用余额(元)", "AmountAvailable"),
                           ("赠送余额(元)", "AmountFree"), ("冻结金额(元)", "AmountFreeze")):
            rows.append({"item": label, "value": _text(info.get(key))})
    else:
        # 子账号通常没有账单权限；如实写明拿不到，而不是把这几行悄悄省掉
        rows.append({"item": "账户余额", "value": f"查询失败：{balance_error}"})

    regions, region_error = _visible_regions(client, public_key, private_key)
    rows.append({
        "item": "可见地域",
        "value": f"{len(regions)} 个" if regions else f"查询失败：{region_error or '无可见地域'}",
    })
    return _list_result(rows, _KV_COLUMNS, "未取到账号信息")


def _run_list_regions(client, public_key: str, private_key: str) -> dict:
    """地域列表（只读）。"""
    payload, error, _ = _request(client, public_key, private_key, "GetRegion")
    if payload is None:
        return {"ok": False, "error": error}
    items = _as_list(payload, "Regions")
    if items is None:
        return {"ok": False, "error": "响应里没有地域列表（Regions），无法解析"}
    return _list_result([_region_row(item) for item in items], _REGION_COLUMNS,
                        "该账号下没有可见的地域")


def _run_list_projects(client, public_key: str, private_key: str) -> dict:
    """项目列表（只读）。"""
    payload, error, _ = _request(client, public_key, private_key, "GetProjectList")
    if payload is None:
        return {"ok": False, "error": error}
    items = _as_list(payload, "ProjectSet")
    if items is None:
        return {"ok": False, "error": "响应里没有项目列表（ProjectSet），无法解析"}
    return _list_result([_project_row(item) for item in items], _PROJECT_COLUMNS,
                        "该账号下没有项目")


def _fetch_region(client, public_key: str, private_key: str, action: str, key: str,
                  region: str, project_id: str, builder):
    """拉一个地域的资源，返回 ``(rows, total, error)``。

    ``total`` 取上游的 ``TotalCount``（拿不到就退化成 ``len(rows)``）；它与 ``len(rows)``
    不等就说明被 ``Limit`` 截断了，调用方据此把 ``has_more`` 置真。
    """
    extra = {"Region": region, "Limit": _PAGE_SIZE, "Offset": 0}
    if project_id:
        extra["ProjectId"] = project_id
    payload, error, _ = _request(client, public_key, private_key, action, extra)
    if payload is None:
        return None, 0, error
    items = _as_list(payload, key)
    if items is None:
        return None, 0, f"响应里没有 {key}，无法解析"
    rows = [builder(item, region) for item in items]
    total = _as_int(payload.get("TotalCount"))
    return rows, (total if total is not None else len(rows)), ""


def _run_resources(client, public_key: str, private_key: str, *, action: str, key: str,
                   region: str, project_id: str, builder, columns, empty_text: str) -> dict:
    """按地域拉一份资源列表（只读）。``region`` 留空 → 并发枚举全部可见地域。"""
    if region:
        rows, total, error = _fetch_region(client, public_key, private_key,
                                           action, key, region, project_id, builder)
        if rows is None:
            return {"ok": False, "error": error}
        return _list_result(rows, columns, empty_text, total=total, has_more=len(rows) < total)

    regions, error = _visible_regions(client, public_key, private_key)
    if not regions:
        return {"ok": False, "error": error or "该账号下没有可见的地域，无法枚举"}

    def worker(one):
        return _fetch_region(client, public_key, private_key, action, key, one, project_id, builder)

    rows: list[dict] = []
    totals: list[int] = []
    failures: list[str] = []
    for one, result, exc_name in _sweep(regions, worker):
        if exc_name:
            failures.append(f"{one}（{exc_name}）")
            continue
        got, total, err = result
        if got is None:
            failures.append(f"{one}（{err}）")
            continue
        rows.extend(got)
        totals.append(total)

    if failures and not rows:
        return {"ok": False,
                "error": f"{len(failures)} 个地域查询全部失败：" + "；".join(failures[:5])}
    if failures:
        # 部分地域没查成（子账号在部分地域无权限是最常见的原因）。表格里放不下说明，
        # 就把「这一份不是全部」写进 empty_text 并落日志 —— 绝不让缺失的地域静默消失。
        log(f"{action} 有 {len(failures)} 个地域查询失败：" + "；".join(failures))
        empty_text = (f"{empty_text}（提示：{'、'.join(failures[:3])} 等地查询失败，"
                      f"本次结果不完整，明细见系统日志）")
    total = sum(totals)
    return _list_result(rows, columns, empty_text, total=total, has_more=len(rows) < total)


# ── 操作声明 ─────────────────────────────────────────────

#: 地域 / 项目两个选填参数，三个资源操作共用同一份声明
_REGION_FIELD = {
    "key": "region", "label": "地域", "type": "text", "required": False,
    "default": DEFAULT_REGION,
    "placeholder": f"如 {DEFAULT_REGION}；留空则遍历全部可见地域",
}
_PROJECT_FIELD = {
    "key": "project_id", "label": "项目 ID", "type": "text", "required": False,
    "placeholder": "选填，如 org-xxxxxx；留空为默认项目",
}


def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据不完整时返回空列表）。"""
    data = data or {}
    if not (_field(data, "public_key") and _field(data, "private_key")):
        return []
    return [
        {
            "id": "whoami",
            "name": "账号信息",
            "kind": "list",
            "group": "账号",
            "order": 10,
            "description": (
                "GetUserInfo + GetBalance + GetRegion（均只读）：账号名称 / 联系方式 / "
                "实名状态，以及账户余额与可见地域数"
            ),
            "columns": _KV_COLUMNS,
            "empty_text": "未取到账号信息",
        },
        {
            "id": "list_regions",
            "name": "地域列表",
            "kind": "list",
            "group": "地域",
            "order": 20,
            "description": (
                "GetRegion（只读）：该账号在各地域的数据中心编号、名称、默认地域与可用区；"
                "每行可下钻到该地域的云主机 / 弹性 IP / 防火墙"
            ),
            "columns": _REGION_COLUMNS,
            "row_actions": _REGION_ROW_ACTIONS,
            "empty_text": "该账号下没有可见的地域",
        },
        {
            "id": "list_projects",
            "name": "项目列表",
            "kind": "list",
            "group": "账号",
            "order": 30,
            "description": (
                "GetProjectList（只读）：项目 ID / 名称 / 成员数 / 资源数 / 上级项目。"
                "资源操作里的「项目 ID」就填这里的值"
            ),
            "columns": _PROJECT_COLUMNS,
            "empty_text": "该账号下没有项目",
        },
        {
            "id": "list_hosts",
            "name": "云主机列表",
            "kind": "list",
            "group": "计算",
            "order": 40,
            "description": (
                "DescribeUHostInstance（只读）：主机 ID / 名称 / 状态 / 公私网 IP / 规格 / "
                "计费方式 / 到期时间。单地域上限 100 台，超出时表头会标注「仅显示部分」"
            ),
            "columns": _HOST_COLUMNS,
            "fields": [_REGION_FIELD, _PROJECT_FIELD],
            "submit": "查询",
            "empty_text": "该账号下没有云主机",
        },
        {
            "id": "list_eips",
            "name": "弹性 IP 列表",
            "kind": "list",
            "group": "网络",
            "order": 50,
            "description": (
                "DescribeEIP（只读）：EIP ID / 地址 / 状态 / 带宽 / 付费模式与绑定资源"
            ),
            "columns": _EIP_COLUMNS,
            "fields": [_REGION_FIELD, _PROJECT_FIELD],
            "submit": "查询",
            "empty_text": "该账号下没有弹性 IP",
        },
        {
            "id": "list_firewalls",
            "name": "防火墙列表",
            "kind": "list",
            "group": "网络",
            "order": 60,
            "description": (
                "DescribeFirewall（只读）：防火墙 ID / 名称 / 类型 / 规则数 / 绑定资源数 / 标签"
            ),
            "columns": _FIREWALL_COLUMNS,
            "fields": [_REGION_FIELD, _PROJECT_FIELD],
            "submit": "查询",
            "empty_text": "该账号下没有防火墙",
        },
    ]


#: 操作 id → 处理函数。处理函数统一 ``(client, public_key, private_key, params)``。
_HANDLERS = {
    "whoami": lambda client, pk, sk, params: _run_whoami(client, pk, sk),
    "list_regions": lambda client, pk, sk, params: _run_list_regions(client, pk, sk),
    "list_projects": lambda client, pk, sk, params: _run_list_projects(client, pk, sk),
    "list_hosts": lambda client, pk, sk, params: _run_resources(
        client, pk, sk, action="DescribeUHostInstance", key="UHostSet",
        region=_field(params, "region"), project_id=_field(params, "project_id"),
        builder=_host_row, columns=_HOST_COLUMNS, empty_text="该账号下没有云主机"),
    "list_eips": lambda client, pk, sk, params: _run_resources(
        client, pk, sk, action="DescribeEIP", key="EIPSet",
        region=_field(params, "region"), project_id=_field(params, "project_id"),
        builder=_eip_row, columns=_EIP_COLUMNS, empty_text="该账号下没有弹性 IP"),
    "list_firewalls": lambda client, pk, sk, params: _run_resources(
        client, pk, sk, action="DescribeFirewall", key="DataSet",
        region=_field(params, "region"), project_id=_field(params, "project_id"),
        builder=_firewall_row, columns=_FIREWALL_COLUMNS, empty_text="该账号下没有防火墙"),
}


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行操作（**全部只读**，绝不产生远端变更）。

    ``client`` 在**本线程**建好后交给处理函数，枚举地域时由工作线程共用同一个
    client —— 代理是 ``threading.local`` 的，工作线程里各自 ``make_client()``
    会丢掉本次凭据配置的代理（详见 ``_sweep``）。
    """
    params = params or {}
    data = data or {}
    try:
        public_key = _field(data, "public_key")
        private_key = _field(data, "private_key")
        if not public_key or not private_key:
            return {"ok": False, "error": "PublicKey 或 PrivateKey 为空"}

        handler = _HANDLERS.get(action_id)
        if handler is None:
            return {"ok": False, "error": f"未知操作：{action_id}"}

        with make_client() as client:
            return handler(client, public_key, private_key, params)
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        # 只回异常类型名：httpx 的异常文本含完整请求 URL 与请求头，会把公私钥泄进界面
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


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
    """只读探针：``POST https://api.ucloud.cn`` 的 ``GetRegion``。

    零副作用 —— 该接口只返回「当前账号在各数据中心的权限」这一份只读清单，
    不创建 / 变更任何资源，也不签发任何令牌。

    为什么是 ``GetRegion`` 而不是 ``ListRegions``：后者不在官方 SDK 的 Action 清单里，
    网关会以 ``RetCode=161``（Action 不存在）拒掉 —— 拿它做探针会把有效凭据全判成异常。
    这条由官方 SDK 的服务方法清单核实，假密钥实测复现不了（签名校验先于 Action 校验，
    未注册密钥一律先撞 171），详见模块 docstring。

    判读交给 ``signer.interpret()``，与「操作」共用同一套 ``RetCode`` 分类；
    ``account_info`` 里补上可见地域 / 可用区数量，它在这个界面上是「这把 Key 真的
    通了」最直观的证据。
    """
    data = data or {}
    missing = [label for key, label in REQUIRED if not _field(data, key)]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    public_key = _field(data, "public_key")
    private_key = _field(data, "private_key")
    log(f"校验 UCloud 凭据 {mask_secret(public_key)}"
        f"（{signer.DEFAULT_ACTION} @ {API_ENDPOINT}）")

    method, url, headers, body = signer.sign_probe(public_key, private_key)
    with make_client() as client:
        resp = client.request(method, url, headers=headers, content=body)

    status, message = _classify(resp, public_key, private_key)
    return VerifyResult(status=status, message=message, account_info=_probe_account(resp))


def _probe_account(resp) -> dict | None:
    """从探针回包里提炼能进「账号信息」的那一点点只读事实（拿不到就不填）。"""
    payload = safe_json(resp)
    items = _as_list(payload, "Regions") if payload is not None else None
    if not items:
        return None
    zones = {zone.strip() for item in items for zone in _text(item.get("Zone")).split(",")}
    zones.discard("")
    return {"可见地域": f"{len(items)} 个", "可见可用区": f"{len(zones)} 个"}


# ── 权限探测（capabilities）─────────────────────────────

def capabilities(data: dict) -> dict:
    """报告当前 PublicKey/PrivateKey **真正能用**的操作（只读探测，拿不准就抛异常）。

    探测用的就是 ``verify`` 那个探针（``GetRegion``），所以结论只有三种：

    - 明确成功 → 全部操作可用；
    - 明确被拒（``invalid`` / ``restricted``）→ 不返回任何操作，并说明原因；
    - 探针本身没跑通（网络抖动 / 响应看不懂）→ **抛异常**，交给框架按
      「未知 → 全部可用」兜底。探测抖一下就把整块功能藏起来，比不探测还糟。
    """
    data = data or {}
    public_key = _field(data, "public_key")
    private_key = _field(data, "private_key")
    if not public_key or not private_key:
        return {"available": [], "note": "缺少 PublicKey 或 PrivateKey"}

    method, url, headers, body = signer.sign_probe(public_key, private_key)
    with make_client() as client:
        resp = client.request(method, url, headers=headers, content=body)
    status, message = _classify(resp, public_key, private_key)

    if status == "valid":
        return {"available": list(_HANDLERS), "note": "PublicKey/PrivateKey 有效"}
    if status in ("invalid", "restricted"):
        # 探针是**明确**被拒（不是「拿不准」），那就如实隐藏：这把 Key 当前确实用不了
        return {"available": [], "note": message}
    raise RuntimeError("UCloud 权限探测失败（网络异常或响应无法识别）")


def get_display_summary(data) -> str:
    data = data or {}
    detail = _field(data, DISPLAY_FIELD)
    return f"{NAME} | {mask_secret(detail)}" if detail else NAME
