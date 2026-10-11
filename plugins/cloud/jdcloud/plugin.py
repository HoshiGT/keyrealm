"""京东云 AccessKey 凭据验证插件（plugins/cloud/ 类别下的独立子插件）。

京东云 OpenAPI 统一是 **HTTP 状态码 + JSON 信封**：成功体 ``{"requestId": ..., "result": {...}}``，
失败体 ``{"requestId": ..., "error": {"status": .., "code": .., "message": ..}}``。判定走
``signer.interpret()``（按错误码分档，非按状态码猜），本插件不另立一套口径。

签名是自研的 ``JDCLOUD3-HMAC-SHA256``（HMAC 四段派生 + scope ``日期/地域/服务/jdcloud3_request``），
实现在同目录 ``jdcloud_signer.py``，已用官方 SDK 的固定向量离线校准。它有两条容易踩的地方，本插件
在调用时已经处理：

- **签名 scope 里的地域「按接口」定，不是按账号定**。路径含 ``{regionId}`` 的接口用调用方给的
  地域；路径不含的（如云物理服务器的 ``/regions``）必须用 ``jdcloud-api``——官方 SDK 的
  ``__get_region_id`` 在参数里取不到 regionId 时就是返回这个字面量。填错不会提示「参数错误」，
  只会得到 SignatureDoesNotMatch。该逻辑收在 ``signer.sign_probe()`` 里。
- **AK/SK 不出现在 URL 里**（全在 Authorization 头），但 ``interpret()`` 的失败消息会带上游
  回显的原文，可能含 Credential 段。因此所有对外文本都先过 ``_redact()`` 洗掉 AK/SK，
  异常路径只回显异常类型名，绝不拼 ``str(exc)``——httpx 的异常文本里是完整请求 URL。

除验证外，本插件还提供以下**只读**操作（actions / run_action）：

- ``whoami``             调用者身份概览：AccessKey 记录、逐服务的只读连通性
- ``list_access_keys``   主账号 AccessKey 列表（**永不渲染 accessKeySecret**）
- ``list_regions``       地域与可用区列表（云物理服务器 DescribeRegiones）
- ``list_instances``     云主机实例列表（按地域）
- ``list_disks``         云硬盘列表（按地域）

参考文档：https://docs.jdcloud.com/cn/iam/api/describeuseraccesskeys
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

from backend.plugin_sdk import (
    VerifyResult,
    body_snippet,
    guarded,
    log,
    make_client,
    mask_secret,
    safe_json,
)

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
    """加载同目录的 ``jdcloud_signer.py``（JDCLOUD3-HMAC-SHA256 签名实现）。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jdcloud_signer.py")
    spec = importlib.util.spec_from_file_location("_vault_jdcloud_signer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载京东云签名模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vault_jdcloud_signer"] = module
    spec.loader.exec_module(module)
    return module
signer = _load_signer()

NAME = "京东云"
DISPLAY_FIELD = "access_key_id"
DEFAULT_REGION = "cn-north-1"
REQUIRED = (("access_key_id", "AccessKey ID"), ("secret_access_key", "AccessKey Secret"))

# 地域代码会被拼进 URL 路径（``/regions/{regionId}/instances``），格式不对就不能往接口上打。
# 存库前 validate() 已拦一道，这里防的是手改过的老记录。
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

# 单条探测的超时（秒）：capabilities / whoami 要连探 4 个服务，逐个给满 20 秒会让
# 「刷新按钮」等太久；探测本身就是「快问快答」，超了就算拿不准（fail-open）。
_PROBE_TIMEOUT = 8.0

# 官方文档里的常用地域代码，用于给「地域」输入框做提示（不参与任何判定，纯提示文本）
_COMMON_REGIONS = "cn-north-1（华北-北京）、cn-east-2（华东-上海）、cn-south-1（华南-广州）、cn-east-1（华东-宿迁）"


# 云主机 / 云硬盘的**页码式**分页参数（接口没有游标，官方 pageSize 取值 [10,100]）。
# 只取第一页，剩余量靠响应里的 totalCount 如实报给前端，不静默截断。
_PAGE_NUMBER = 1
_PAGE_SIZE = 100
_PAGE_QUERY = f"pageNumber={_PAGE_NUMBER}&pageSize={_PAGE_SIZE}"


# ── 探测分档 ────────────────────────────────────────────
# 与 huaweicloud 插件同构的四态：明确成功 / 明确无权限 / 明确密钥废 / 拿不准。
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

# 逐只读操作探哪个接口。探的是「这组 AK 对这个服务有没有读权限」，不是「账号有没有资源」。
# (action_id, signer 接口名, 用哪个地域发探测)
_SERVICE_PROBES = (
    ("list_access_keys", "DescribeUserAccessKeys", DEFAULT_REGION),
    ("list_regions", "DescribeRegiones", DEFAULT_REGION),
    ("list_instances", "DescribeInstances", DEFAULT_REGION),
    ("list_disks", "DescribeDisks", DEFAULT_REGION),
)

_VERDICT_TO_PROBE = {
    "valid": _PROBE_OK,
    "invalid": _PROBE_BAD_CRED,
    "restricted": _PROBE_DENIED,
}

# 探测结果给人看时的服务名（capabilities 的 note 与 whoami 的行共用）
_SERVICE_LABELS = {
    "list_access_keys": "IAM 身份",
    "list_regions": "地域列表",
    "list_instances": "云主机",
    "list_disks": "云硬盘",
}


# ── 通用小工具 ──────────────────────────────────────────

def _field(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def _scalar(value) -> str:
    """标量 → 展示文本；None / 空列表 / 空字典统一样式为 ``—``。"""
    if value is None or value == [] or value == {}:
        return "—"
    if isinstance(value, (list, tuple)):
        parts = [_scalar(item) for item in value]
        parts = [part for part in parts if part != "—"]
        return "、".join(parts) if parts else "—"
    if isinstance(value, dict):
        parts = [f"{key}={_scalar(val)}" for key, val in value.items()]
        return "、".join(parts) if parts else "—"
    text = str(value).strip()
    return text or "—"


def _pick(item: dict, columns) -> dict:
    return {column["key"]: _scalar(item.get(column["key"])) for column in columns}


def _redact(text: str, *secrets: str) -> str:
    """把可能被上游回显出来的凭据替换掉，任何路径都不回显 AK/SK。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _error_node(payload) -> dict:
    node = payload.get("error") if isinstance(payload, dict) else None
    return node if isinstance(node, dict) else {}


def _error_code(resp) -> str:
    """取上游错误码原文（判断分档用，不脱敏也无妨——码本身不含密钥）。"""
    return str(_error_node(safe_json(resp)).get("code") or "").strip()


def _failure(resp, ak: str, sk: str) -> str:
    """把失败响应转成一句脱敏后的错误信息（错误体形状固定，做兜底）。"""
    node = _error_node(safe_json(resp))
    detail = str(node.get("message") or node.get("code") or "") or body_snippet(resp)
    return f"请求失败 (HTTP {resp.status_code})：{_redact(detail, ak, sk)}"


def _request(client, ak: str, sk: str, action: str, region: str, query: str = ""):
    """签名并发出一条只读请求，返回 ``httpx.Response``。

    签名器只产出 ``(method, url, headers, body)``，**网络一律由这里的 ``client`` 发**：
    它必须是调用方用 ``make_client()`` 建好的，只有那样才带得上该条凭据配置的代理。
    """
    method, url, headers, body = signer.sign_probe(
        ak, sk, region=region, action=action, query=query
    )
    return client.request(method, url, headers=headers, content=body or None)


def _probe_one(client, ak: str, sk: str, action: str, region: str) -> str:
    """探一个接口，返回四态之一；**不抛异常**（拿不准一律 unknown）。"""
    try:
        resp = _request(client, ak, sk, action, region)
    except Exception as exc:  # noqa: BLE001 - 探测失败即「拿不准」，不能连累其它探测
        log(f"京东云权限探测失败 {action}: {type(exc).__name__}")
        return _PROBE_UNKNOWN
    verdict = str(signer.interpret(resp.status_code, resp.text).get("verdict") or "")
    return _VERDICT_TO_PROBE.get(verdict, _PROBE_UNKNOWN)


def _probe_all(ak: str, sk: str) -> dict:
    """逐服务只读探测 → ``{action_id: 四态}``。"""
    status: dict[str, str] = {}
    try:
        with make_client(request_timeout=_PROBE_TIMEOUT) as client:
            for action_id, probe, region in _SERVICE_PROBES:
                status[action_id] = _probe_one(client, ak, sk, probe, region)
    except Exception as exc:  # noqa: BLE001 - 连客户端都建不起来（代理配置坏了等）
        log(f"京东云权限探测无法建立连接: {type(exc).__name__}")
        for action_id, _probe, _region in _SERVICE_PROBES:
            status.setdefault(action_id, _PROBE_UNKNOWN)
    return status


# ── 列定义 ──────────────────────────────────────────────

_KV_COLUMNS = [
    {"key": "item", "label": "项目", "width": 160},
    {"key": "value", "label": "值"},
]

# 注意：AccessKey 列表**只列 ID 与状态**，绝不渲染响应里的 accessKeySecret
_ACCESS_KEY_COLUMNS = [
    {"key": "accessKey", "label": "AccessKey ID", "mono": True},
    {"key": "state", "label": "状态", "width": 90, "type": "tag"},
    {"key": "current", "label": "当前凭据", "width": 100, "type": "tag"},
    {"key": "createTime", "label": "创建时间", "width": 180},
]

_REGION_COLUMNS = [
    {"key": "region", "label": "地域代码", "mono": True, "width": 130},
    {"key": "regionName", "label": "地域名称"},
    {"key": "azs", "label": "可用区", "mono": True},
]

_INSTANCE_COLUMNS = [
    {"key": "instanceId", "label": "实例 ID", "mono": True},
    {"key": "instanceName", "label": "名称"},
    {"key": "status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "az", "label": "可用区", "width": 110},
    {"key": "instanceType", "label": "规格", "width": 150},
    {"key": "privateIpAddress", "label": "内网 IP", "width": 130, "mono": True},
    {"key": "elasticIpAddress", "label": "公网 IP", "width": 130, "mono": True},
]

_DISK_COLUMNS = [
    {"key": "diskId", "label": "云硬盘 ID", "mono": True},
    {"key": "name", "label": "名称"},
    {"key": "diskType", "label": "类型", "width": 130, "type": "tag"},
    {"key": "diskSizeGB", "label": "容量(GiB)", "width": 100},
    {"key": "status", "label": "状态", "width": 100, "type": "tag"},
    {"key": "az", "label": "可用区", "width": 110},
    {"key": "createTime", "label": "创建时间", "width": 180},
]

# 地域列表行内「实例」下钻到 list_instances（region 作为 params 传下去预填表单）
_REGION_ROW_ACTION = {
    "id": "list_instances",
    "label": "实例",
    "params": {"region": "$row.region"},
}

_REGION_FIELD = {
    "key": "region",
    "label": "地域",
    "placeholder": f"默认 {DEFAULT_REGION}，如 {DEFAULT_REGION}",
    "hint": f"该地域会同时进 URL 路径与签名作用域。常用：{_COMMON_REGIONS}。",
}


def _rows(rows: list, columns, empty_text: str, total: int | None = None,
          has_more: bool = False) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows) if total is None else total,
        "has_more": has_more,
        "empty_text": empty_text,
    }


def _paginated_has_more(page_number: int, page_size: int | None,
                        total: int | None, row_count: int) -> bool:
    """是否还有下一页 —— **只信上游给的翻页信号，不靠 total 反推**。

    京东云这两类接口是**页码式**分页（``pageNumber`` / ``pageSize``，没有 NextToken 之类游标），
    官方文档写明 ``totalCount`` 是「本次查询可匹配到的总记录数，使用者需要结合 pageNumber 和
    pageSize 计算是否可以继续分页」—— 所以：

    - ``totalCount`` 拿得到 → 按文档的公式算（``pageNumber * pageSize < totalCount``）；
    - ``totalCount`` 拿不到 → 退回「本页是否被填满」（满页 ≈ 后面还有），
      **宁可多说一句「仅显示部分」，也不能把截断说成「已取全」**；
    - 接口本身不分页（``page_size is None``，一次返回全量）→ 恒 False。

    ``has_more`` 是界面上「（仅显示部分）」的唯一依据，写错会让截断结果被界面说成完整的。
    """
    if page_size is None:
        return False
    if total is not None:
        return page_number * page_size < total
    return row_count >= page_size


def _list_result(resp, ak: str, sk: str, result_key: str, columns, empty_text: str,
                 build, page_number: int = 1, page_size: int | None = None) -> dict:
    """列表类接口的公共后半段：解析 → 取 ``result[result_key]`` → 交给 ``build`` 组行。

    ``build(items) -> list[dict]``。``total`` 优先用响应里的 ``totalCount``（云主机 / 云硬盘
    都有），没有就退回实际行数；``has_more`` 见 ``_paginated_has_more``，与 ``total`` 无关。
    """
    payload = safe_json(resp)
    if payload is None:
        return {"ok": False, "error": f"响应无法解析 (HTTP {resp.status_code})"}
    if resp.status_code != 200 or _error_node(payload):
        return {"ok": False, "error": _failure(resp, ak, sk)}
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    items = result.get(result_key)
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    try:
        total = int(result.get("totalCount"))
    except (TypeError, ValueError):
        total = None
    built = build(items)
    return _rows(built, columns, empty_text, total,
                 _paginated_has_more(page_number, page_size, total, len(built)))


# ── 操作实现 ────────────────────────────────────────────

def _access_key_rows(items: list, ak: str) -> list:
    """AccessKey 记录 → 行。**刻意不取 accessKeySecret**。"""
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        # state: 0 禁用 / 1 启用；yn: 0 已删除 / 1 有效
        state = _scalar(item.get("state"))
        if str(item.get("yn")) == "0":
            state = "已删除"
        elif state == "1":
            state = "启用"
        elif state == "0":
            state = "禁用"
        rows.append({
            "accessKey": _scalar(item.get("accessKey")),
            "state": state,
            "current": "是" if str(item.get("accessKey")) == ak else "",
            "createTime": _scalar(item.get("createTime")),
        })
    return rows


def _run_whoami(ak: str, sk: str) -> dict:
    """身份概览：AccessKey 记录 + 逐服务只读连通性（各接口独立只读调用）。"""
    rows: list[dict] = [{"item": "当前 AccessKey", "value": mask_secret(ak)}]

    with make_client(request_timeout=_PROBE_TIMEOUT) as client:
        resp = None
        try:
            resp = _request(client, ak, sk, signer.DEFAULT_ACTION, DEFAULT_REGION)
        except Exception as exc:  # noqa: BLE001 - 身份接口连不上也要给出可读结论
            rows.append({"item": "身份接口", "value": f"请求失败（{type(exc).__name__}）"})

        if resp is not None:
            verdict = signer.interpret(resp.status_code, resp.text)
            rows.append({"item": "身份接口",
                         "value": f"HTTP {resp.status_code} · {verdict.get('message') or ''}"})
            payload = safe_json(resp)
            result = payload.get("result") if isinstance(payload, dict) else None
            items = result.get("userAccessKeys") if isinstance(result, dict) else None
            if isinstance(items, list):
                enabled = sum(1 for item in items
                              if isinstance(item, dict) and str(item.get("state")) == "1")
                rows.append({"item": "主账号 AccessKey",
                             "value": f"{len(items)} 个（启用 {enabled}，其余禁用或已删除）"})
                rows.append({"item": "当前凭据在列表内",
                             "value": "是" if any(str(item.get("accessKey")) == ak
                                                  for item in items if isinstance(item, dict))
                             else "否（可能是子用户或该 AK 已删除）"})

        # 逐服务连通性：与 capabilities() 同源，这里摊开成给人看的文本
        rights = []
        blocked = []
        bad_cred = False
        for action_id, probe, region in _SERVICE_PROBES:
            value = _probe_one(client, ak, sk, probe, region)
            if value == _PROBE_OK:
                rights.append(_SERVICE_LABELS[action_id])
            elif value == _PROBE_DENIED:
                blocked.append(_SERVICE_LABELS[action_id])
            elif value == _PROBE_BAD_CRED:
                bad_cred = True
        if bad_cred:
            # 同一对 AK 在别处必然也被拒，逐服务列「无权限」只会误导
            rows.append({"item": "可读服务", "value": "AccessKey 无效或已禁用，任何接口都会被拒"})
        else:
            if rights:
                rows.append({"item": "可读服务", "value": "、".join(rights)})
            if blocked:
                rows.append({"item": "无读权限的服务", "value": "、".join(blocked)})
            if not rights and not blocked:
                rows.append({"item": "可读服务", "value": "未探测出结果（网络异常或响应无法识别）"})

    return _rows(rows, _KV_COLUMNS, "未获取到身份信息")


def _run_list_access_keys(ak: str, sk: str) -> dict:
    with make_client() as client:
        resp = _request(client, ak, sk, "DescribeUserAccessKeys", DEFAULT_REGION)
    return _list_result(resp, ak, sk, "userAccessKeys", _ACCESS_KEY_COLUMNS,
                        "该账号下没有 AccessKey",
                        lambda items: _access_key_rows(items, ak))


def _run_list_regions(ak: str, sk: str) -> dict:
    """地域与可用区（云物理服务器 DescribeRegiones）。

    这个接口的路径里**没有** ``{regionId}``，签名作用域按官方 SDK 的规则取 ``jdcloud-api``
    （见 signer.GLOBAL_SCOPE_REGION），所以不要在这里传 cn-north-1。
    """
    def _wrap(items: list) -> list:
        rows = []
        for item in items:
            azs = item.get("azs")
            names = [str(az.get("az") or "").strip()
                     for az in (azs if isinstance(azs, list) else []) if isinstance(az, dict)]
            rows.append({
                "region": _scalar(item.get("region")),
                "regionName": _scalar(item.get("regionName")),
                "azs": "、".join(name for name in names if name) or "—",
            })
        return rows

    with make_client() as client:
        resp = _request(client, ak, sk, "DescribeRegiones", DEFAULT_REGION)
    return _list_result(resp, ak, sk, "regions", _REGION_COLUMNS,
                        "未查询到地域列表", _wrap)


def _region_of(params: dict, default: str) -> str:
    """取本次调用要用的地域：表单 > 凭据默认 > 内置缺省。"""
    return (str((params or {}).get("region") or "").strip() or default or DEFAULT_REGION)


def _run_list_instances(ak: str, sk: str, params: dict, default: str) -> dict:
    region = _region_of(params, default)
    if not _REGION_RE.match(region):
        return {"ok": False, "error": f"地域格式不正确：{region}"}
    # 页码式分页，只取第一页；totalCount 与实际行数一起交给 _list_result 定 has_more
    with make_client() as client:
        resp = _request(client, ak, sk, "DescribeInstances", region, query=_PAGE_QUERY)
    return _list_result(resp, ak, sk, "instances", _INSTANCE_COLUMNS,
                        f"该地域暂无云主机实例（{region}）",
                        lambda items: [_pick(item, _INSTANCE_COLUMNS) for item in items],
                        page_number=_PAGE_NUMBER, page_size=_PAGE_SIZE)


def _run_list_disks(ak: str, sk: str, params: dict, default: str) -> dict:
    region = _region_of(params, default)
    if not _REGION_RE.match(region):
        return {"ok": False, "error": f"地域格式不正确：{region}"}
    with make_client() as client:
        resp = _request(client, ak, sk, "DescribeDisks", region, query=_PAGE_QUERY)
    return _list_result(resp, ak, sk, "disks", _DISK_COLUMNS,
                        f"该地域暂无云硬盘（{region}）",
                        lambda items: [_pick(item, _DISK_COLUMNS) for item in items],
                        page_number=_PAGE_NUMBER, page_size=_PAGE_SIZE)


# ── 操作声明 ────────────────────────────────────────────

def actions(data: dict) -> list[dict]:
    """声明可用操作（凭据完整时提供只读的身份 / 地域 / 云主机 / 云硬盘查询）。"""
    data = data or {}
    if not (_field(data, DISPLAY_FIELD) and _field(data, "secret_access_key")):
        return []
    return [
        {
            "id": "whoami",
            "name": "查看调用者身份",
            "kind": "list",
            "group": "身份",
            "order": 10,
            "description": "AccessKey 记录概览 + 逐服务的只读连通性探测（不改变任何远端状态）",
            "columns": _KV_COLUMNS,
            "empty_text": "未获取到身份信息",
        },
        {
            "id": "list_access_keys",
            "name": "AccessKey 列表",
            "kind": "list",
            "group": "身份",
            "order": 20,
            "description": "IAM 查询主账号 AccessKey（只读；**不显示 AccessKey Secret**）",
            "columns": _ACCESS_KEY_COLUMNS,
            "empty_text": "该账号下没有 AccessKey",
        },
        {
            "id": "list_regions",
            "name": "地域与可用区",
            "kind": "list",
            "group": "计算",
            "order": 30,
            "description": "云物理服务器 DescribeRegiones；行内可下钻该地域的「实例」",
            "columns": _REGION_COLUMNS,
            "row_actions": [_REGION_ROW_ACTION],
            "empty_text": "未查询到地域列表",
        },
        {
            "id": "list_instances",
            "name": "云主机实例列表",
            "kind": "list",
            "group": "计算",
            "order": 40,
            "description": (f"云主机 DescribeInstances（地域默认 {DEFAULT_REGION}，可直接填，"
                            "或从「地域与可用区」行内进入；单次最多返回 100 条）"),
            "columns": _INSTANCE_COLUMNS,
            # 列表操作也能带 fields：前端渲染成表格上方一行内联表单，点「查询」时并入参数
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "empty_text": f"该地域暂无云主机实例（{DEFAULT_REGION}）",
        },
        {
            "id": "list_disks",
            "name": "云硬盘列表",
            "kind": "list",
            "group": "存储",
            "order": 50,
            "description": (f"云硬盘 DescribeDisks（地域默认 {DEFAULT_REGION}；"
                            "单次最多返回 100 条）"),
            "columns": _DISK_COLUMNS,
            "fields": [_REGION_FIELD],
            "submit": "查询",
            "empty_text": f"该地域暂无云硬盘（{DEFAULT_REGION}）",
        },
    ]


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """执行只读操作；任何异常都转成 ``{"ok": False, "error": ...}``。

    异常消息**只带异常类型名**，不带 ``str(exc)``——httpx 的异常文本里含完整请求 URL，
    与模块 docstring 记录的理由一致。
    """
    data = data or {}
    params = params or {}
    try:
        ak = _field(data, DISPLAY_FIELD)
        sk = _field(data, "secret_access_key")
        if not ak or not sk:
            return {"ok": False, "error": "AccessKey ID 或 AccessKey Secret 为空"}
        default = _field(data, "region") or DEFAULT_REGION

        if action_id == "whoami":
            return _run_whoami(ak, sk)
        if action_id == "list_access_keys":
            return _run_list_access_keys(ak, sk)
        if action_id == "list_regions":
            return _run_list_regions(ak, sk)
        if action_id == "list_instances":
            return _run_list_instances(ak, sk, params, default)
        if action_id == "list_disks":
            return _run_list_disks(ak, sk, params, default)
        return {"ok": False, "error": f"未知操作：{action_id}"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


# ── 能力探测 ────────────────────────────────────────────

def capabilities(data: dict) -> dict:
    """返回当前 AK/SK **真正能用**的操作 id（只读探测，拿不准时抛异常 → fail-open）。

    逐服务探一条只读接口，各自独立 try：

    - 明确成功 → 该操作可用；
    - 明确「无权限 / 账号未开通该产品」→ 隐藏该操作；
    - 明确「密钥无效」→ 相关操作一并隐藏（同一对密钥在别处必然也被拒）；
    - 网络异常、响应看不懂等**探测不了**的情况 → 按可用处理。

    若四个服务全都没探出结果，则直接抛异常，交给框架按「未知 → 全部可用」兜底。
    """
    data = data or {}
    ak = _field(data, DISPLAY_FIELD)
    sk = _field(data, "secret_access_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 AccessKey ID 或 AccessKey Secret"}

    status = _probe_all(ak, sk)

    if all(value == _PROBE_UNKNOWN for value in status.values()):
        # 全都不确定 —— 让框架按「未知」处理，绝不返回空列表把操作全隐藏
        raise RuntimeError("京东云权限探测全部失败（网络异常或响应无法识别）")

    if _PROBE_BAD_CRED in status.values():
        return {"available": [], "note": "AccessKey 无效或已禁用，任何操作都会失败"}

    available = [
        action_id for action_id, value in status.items() if value == _PROBE_OK
    ]
    # whoami 用的是与 list_access_keys 同一个 IAM 接口（_SERVICE_PROBES 里不为它重发一次探测），
    # 所以身份接口能读即代表它可用。**必须显式补进来**：available 是动作白名单，缺了就会把
    # 「查看调用者身份」整个从界面上抹掉。
    if "list_access_keys" in available:
        available.append("whoami")
    denied = [_SERVICE_LABELS.get(action_id, action_id)
              for action_id, value in status.items() if value == _PROBE_DENIED]
    rights = [_SERVICE_LABELS.get(action_id, action_id) for action_id in available]
    if rights and denied:
        note = f"可读：{'、'.join(rights)}；无读权限：{'、'.join(denied)}"
    elif rights:
        note = f"可读：{'、'.join(rights)}"
    else:
        note = "未探测到可用的只读接口权限"
    return {"available": available, "note": note}


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors = [f"{label} 不能为空" for key, label in REQUIRED if not _field(data, key)]
    region = _field(data, "region")
    if region and not _REGION_RE.match(region):
        errors.append("默认地域格式不正确（形如 cn-north-1）")
    return errors


@guarded
def verify(data) -> VerifyResult:
    """用 IAM 列 AccessKey 验证凭据（只读、零副作用）。

    选它的理由：它是**自指**接口（读的是这组 AK 自己的归属信息），不会碰到账号里的任何
    业务资源；且它就在签名器的官方校准集里，签名正确性已被固定向量覆盖。

    判定**直接用签名器 ``interpret()`` 的返回值**，不另立一套口径。唯一要补的是脱敏：
    ``interpret()`` 在失败路径会带上游回显的原文（可能含 Credential 段里的 AK），
    而成功路径的响应体里含 ``accessKeySecret``——两处都不能原样进消息。
    """
    data = data or {}
    missing = [label for key, label in REQUIRED if not _field(data, key)]
    if missing:
        return VerifyResult(status="invalid", message=f"缺少必填字段: {'、'.join(missing)}")

    ak = _field(data, DISPLAY_FIELD)
    sk = _field(data, "secret_access_key")
    region = _field(data, "region") or DEFAULT_REGION
    if not _REGION_RE.match(region):
        # 地域会进 URL 路径与签名作用域，格式不对就别打接口（拿不准 → error，不冤判 invalid）
        return VerifyResult(status="error", message=f"地域格式不正确：{region}")

    log(f"校验京东云凭据 {mask_secret(ak)}（DescribeUserAccessKeys @ {region}）")

    with make_client() as client:
        resp = _request(client, ak, sk, signer.DEFAULT_ACTION, region)

    verdict = signer.interpret(resp.status_code, resp.text)
    status = str(verdict.get("verdict") or "error")
    message = _redact(str(verdict.get("message") or ""), ak, sk)

    return VerifyResult(status=status, message=message,
                        account_info=_account_info(resp, status, ak))


def _account_info(resp, status: str, ak: str) -> dict | None:
    """附加信息列只放**安全且稳定**的两项；响应里的 accessKeySecret 一概不取。"""
    if status != "valid":
        return None
    payload = safe_json(resp)
    result = payload.get("result") if isinstance(payload, dict) else None
    items = result.get("userAccessKeys") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return None
    info = {"主账号 AccessKey": f"{len(items)} 个"}
    mine = [item for item in items
            if isinstance(item, dict) and str(item.get("accessKey")) == ak]
    if mine:
        info["当前 AK 状态"] = "启用" if str(mine[0].get("state")) == "1" else "禁用"
    return info


def get_display_summary(data) -> str:
    data = data or {}
    ak = _field(data, DISPLAY_FIELD)
    return f"{NAME} | {mask_secret(ak)}" if ak else NAME
