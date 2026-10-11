"""百度智能云（BCE）Access Key 凭据插件。

只做**只读**校验与查询，绝不产生任何远端变更。签名不在这里重写：同目录 ``bce_signer.py``
是一份对齐官方 SDK（``bce-sdk-python``，SDK_VERSION=0.9.79）的 BCE V1 签名器，本插件
只负责「用签名器组织出 ``(method, url, headers, body)`` → 经 ``make_client()`` 发出去 →
解析响应」。**签名器自身不发请求**，一切流量都走 ``make_client()``，才带得上当前凭据
配置的代理。

两处容易踩的签名细节（改本文件时不要破坏）：

- **空 body 的 ``Content-Length: 0`` 必须真的发出去**。BCE V1 的 CanonicalHeaders 包含
  ``content-length``，而 httpx 对无 body 的 GET 不会自动补这个头；漏了就是恒定签名失败。
  签名头一律由 ``_bce_request()`` 统一产出，调用方**不要**再覆盖或删除。
- **查询串用签名器自己的 ``canonical_querystring()`` 拼进 URL**，不要用 httpx 的
  ``params=``：后者把空格编成 ``+``，与 CanonicalQueryString 的 ``%20`` 不一致，会签名失败。

判定口径（框架统一规则）：配额用尽 / IP 白名单 / 权限不足 / 账号未开通 / 被禁用
一律判 ``restricted``（凭证有效但受限）；只有明确「AK 不存在 / 签名校验失败 / 密钥非法」
才判 ``invalid``。校验探针取 ``GET https://iam.bj.baidubce.com/v1/user``（IAM 列举子用户）：
该接口无副作用，且服务端**先校验凭证、再路由**，所以即便调用者没有 IAM 读权限也能拿到
「凭证是否存在」的明确答复（实测未知 AK 返回 403 ``IamSignatureInvalid``，并非官方错误码
表里写的 ``InvalidAccessKeyId``）。

本插件提供的操作（``actions``，全部只读、``kind: "list"``）：

- ``list_users``     IAM 子用户列表       ``GET /v1/user``
- ``list_groups``    IAM 用户组列表       ``GET /v1/group``
- ``list_roles``     IAM 角色列表         ``GET /v1/role``
- ``list_policies``  IAM 权限策略列表     ``GET /v1/policy``（可选 policyType / nameFilter）
- ``list_buckets``   BOS 存储桶列表       ``GET https://<区域>.bcebos.com/``
- ``list_objects``   BOS 对象浏览         ``GET https://<桶>.<区域>.bcebos.com/?prefix=&delimiter=/&maxKeys=``

另实现可选的 ``capabilities(data)``：点「操作」前并发只读探测各项接口，只列出这组 AK/SK
**真正能用**的操作。**拿不准一律 fail-open**（按可用处理），只有接口明确回「没权限 /
密钥被拒」才隐藏。

参考文档：https://cloud.baidu.com/doc/Reference/s/njwvz1yfu
参考文档：https://cloud.baidu.com/doc/Reference/s/6k6yfh1he
参考文档：https://cloud.baidu.com/doc/IAM/s/Pjx35b92m
参考文档：https://cloud.baidu.com/doc/IAM/s/mjx35fixq
参考文档：https://cloud.baidu.com/doc/IAM/s/ljx35h8lx
参考文档：https://cloud.baidu.com/doc/IAM/s/ek5eq1zp1
参考文档：https://cloud.baidu.com/doc/IAM/s/Wjx35jxes
参考文档：https://cloud.baidu.com/doc/BOS/s/bkcacfjvi
参考文档：https://cloud.baidu.com/doc/BOS/s/Ekc4epj6m
参考文档：https://cloud.baidu.com/doc/BOS/s/akrqd2wcx
参考文档：https://cloud.baidu.com/doc/IAM/s/Rjx4d0rxo
"""
from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from backend.plugin_sdk import (
    VerifyResult,
    first_list,
    guarded,
    log_debug,
    make_client,
    safe_json,
)

NAME = "百度智能云"
DEFAULT_REGION = "bj"

#: IAM 的服务域名与 URL 前缀（官方 SDK iam_client.py: URL_PREFIX = b"/v1"）
IAM_HOST = "iam.bj.baidubce.com"
IAM_PREFIX = "/v1"

#: IAM 只读接口一律带 Content-Type: application/json（对齐官方 SDK 的每一处调用）
_JSON_TYPE = "application/json"

#: BOS 区域表（官方「区域与访问域名」文档）。值是该区域的中文名，仅用于文档/日志。
#: 服务域名一律 ``<区域>.bcebos.com``，桶级域名 ``<桶>.<区域>.bcebos.com``。
BOS_REGIONS = {
    "bj": "华北-北京",
    "bd": "华北-保定",
    "su": "华东-苏州",
    "gz": "华南-广州",
    "cd": "西南-成都",
    "yq": "阳泉",
    "hkg": "中国香港",
    "fwh": "华中-武汉",
}

#: BOS 桶名规则，逐字取自官方 SDK bos_handler._BUCKET_NAME_REGEX
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")

#: 区域名规则（只允许小写字母/数字/连字符），用于挡住把域名拼歪的输入
_REGION_RE = re.compile(r"^[a-z0-9\-]{1,32}$")

#: BOS 单页对象数上限（官方 ListObjects 的 maxKeys 上限即 1000）
_PAGE_SIZE = 1000

#: 权限探测的单次超时（秒）。探测只为给菜单打标，不能让用户干等。
_PROBE_TIMEOUT = 8.0
_SWEEP_WORKERS = 6

#: 权限探测的四态（与 huaweicloud 等云插件保持同一套口径）
_PROBE_OK = "ok"
_PROBE_DENIED = "denied"
_PROBE_BAD_CRED = "bad_cred"
_PROBE_UNKNOWN = "unknown"

REQUIRED = (("access_key_id", "Access Key ID (AK)"), ("secret_access_key", "Secret Access Key (SK)"))
DISPLAY_FIELD = "access_key_id"


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
    """加载同目录的 ``bce_signer.py``（BCE V1 签名实现）。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bce_signer.py")
    spec = importlib.util.spec_from_file_location("_vault_baiducloud_signer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载百度智能云签名模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_vault_baiducloud_signer"] = module
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
    """把可能出现在接口返回里的密钥抹掉；任何路径都不回显凭据。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _safe_text(response) -> str:
    """取响应正文；解码失败时退回空串（不能因为一个畸形响应把整个操作打挂）。"""
    try:
        return response.text or ""
    except Exception:  # noqa: BLE001 - 解码兜底
        return ""


def _failure(response, ak: str, sk: str, action: str = "请求") -> str:
    """非 2xx 响应的脱敏提示：优先用签名器解析出的错误码与解读。

    **绝不拼 ``str(exc)``**，也不直接把响应原文丢出去 —— 签名器只回 ``Code`` / ``Message``
    与一句中文解读，天然不含请求 URL 与凭据。
    """
    info = signer.interpret(
        response.status_code, _safe_text(response), response.headers.get("content-type", "")
    )
    detail = info.get("message") or f"HTTP {response.status_code}"
    return f"{action}失败：{_redact(detail, ak, sk)}"


# ── 已签名请求 ──────────────────────────────────────────

def _bce_request(client, ak: str, sk: str, host: str, method: str, path: str,
                 params: Optional[dict] = None, json_api: bool = False):
    """构造并发送一个已签名的 BCE 只读请求，返回 ``httpx.Response``。

    签名头由本函数**统一**产出（Host / Content-Length: 0 / x-bce-date / 可选 Content-Type），
    调用方不要再覆盖或删除 —— 尤其 ``Content-Length: 0``：BCE V1 的 CanonicalHeaders 含
    ``content-length``，而 httpx 对无 body 的 GET 不会自动补，漏掉就恒定签名失败。

    ``json_api=True`` 时补 ``Content-Type: application/json``（对齐官方 SDK 的 IAM 调用）。
    该头落在默认签名集合内，因此**必须**在签名之前写好，服务端重算时才一致。
    """
    headers = {
        "Host": host,
        "Content-Length": "0",
        "x-bce-date": signer.canonical_time(),
    }
    if json_api:
        headers["Content-Type"] = _JSON_TYPE
    headers["Authorization"] = signer.sign_authorization(ak, sk, method, path, headers, params)

    # 查询串与签名共用同一份 canonical_querystring()，保证「签的」与「发的」逐字节一致。
    # 不用 httpx 的 params=（它把空格编成 +，与 canonical 的 %20 不符）。
    query = signer.canonical_querystring(params)
    url = "https://%s%s" % (host, path)
    if query:
        url += "?" + query
    return client.request(method, url, headers=headers)


def _classify(status_code: int, text: str, content_type: str) -> str:
    """把一次探针响应归到四态之一（供 ``capabilities`` 用）。"""
    verdict = signer.interpret(status_code, text, content_type).get("verdict")
    if verdict == "valid":
        return _PROBE_OK
    if verdict == "invalid":
        return _PROBE_BAD_CRED
    if verdict == "restricted":
        # 签名已通过、只是没这个资源的权限 → 明确「没权限」
        return _PROBE_DENIED
    # 其余（时间偏差 / 网关错误 / 错误体无法解析）一律「拿不准」，由调用方 fail-open
    return _PROBE_UNKNOWN


# ── 行/列定义 ───────────────────────────────────────────

_USER_COLUMNS = [
    {"key": "name", "label": "用户名", "mono": True},
    {"key": "id", "label": "用户 ID", "mono": True},
    {"key": "status", "label": "状态", "width": 90, "type": "tag"},
    {"key": "description", "label": "描述"},
    {"key": "createTime", "label": "创建时间", "width": 200},
    {"key": "lastLoginTime", "label": "最后登录", "width": 200},
]

_GROUP_COLUMNS = [
    {"key": "name", "label": "组名", "mono": True},
    {"key": "id", "label": "组 ID", "mono": True},
    {"key": "description", "label": "描述"},
    {"key": "createTime", "label": "创建时间", "width": 200},
]

_ROLE_COLUMNS = [
    {"key": "name", "label": "角色名", "mono": True},
    {"key": "id", "label": "角色 ID", "mono": True},
    {"key": "description", "label": "描述"},
    {"key": "createTime", "label": "创建时间", "width": 200},
]

_POLICY_COLUMNS = [
    {"key": "name", "label": "策略名", "mono": True},
    {"key": "type", "label": "类型", "width": 90, "type": "tag"},
    {"key": "id", "label": "策略 ID", "mono": True},
    {"key": "description", "label": "描述"},
    {"key": "createTime", "label": "创建时间", "width": 200},
]

_BUCKET_COLUMNS = [
    {"key": "name", "label": "桶名", "mono": True},
    {"key": "location", "label": "区域", "width": 100},
    {"key": "creationDate", "label": "创建时间", "width": 200},
    {"key": "enableMultiAz", "label": "多 AZ", "width": 90},
]

_OBJECT_COLUMNS = [
    {"key": "key", "label": "对象 Key", "mono": True},
    {"key": "type", "label": "类型", "width": 90, "type": "tag"},
    {"key": "size", "label": "大小", "width": 110, "type": "number"},
    {"key": "lastModified", "label": "最后修改", "width": 200},
]

# 桶列表行内「浏览」下钻到 list_objects。region 取该桶自己的 location，
# 这样跨区域的桶也能一次点对（BOS 的对象操作只在桶所在区域生效）。
_BUCKET_ROW_ACTION = {
    "id": "list_objects",
    "label": "浏览",
    "params": {"bucket": "$row.name", "region": "$row.location"},
}

# 对象列表的行内按钮：文件夹行有 folderPath（「进入」可用），文件行有 fileKey。
# 对方没有的键由前端自动置灰 —— 绝不会传 undefined 过来。
_OBJECT_ROW_ACTIONS = [
    {
        "id": "list_objects",
        "label": "进入",
        "params": {"bucket": "$row.bucket", "region": "$row.region", "prefix": "$row.folderPath"},
    },
]

_IAM_FIELDS = [
    {
        "key": "policy_type",
        "label": "策略类型",
        "type": "select",
        "options": [
            {"value": "Custom", "label": "自定义策略 (Custom)"},
            {"value": "System", "label": "系统策略 (System)"},
        ],
        "default": "Custom",
        "hint": "官方语义：不传时服务端默认只返回自定义策略。",
    },
    {"key": "name_filter", "label": "名称过滤", "placeholder": "留空为全部，如 test"},
]

_OBJECT_FIELDS = [
    {"key": "bucket", "label": "桶名", "required": True, "placeholder": "如 my-bucket"},
    {"key": "region", "label": "区域", "placeholder": f"默认 {DEFAULT_REGION}，如 gz"},
    {"key": "prefix", "label": "前缀", "placeholder": "留空为桶根目录，如 dir/"},
]


# ── 校验 / 验证 ─────────────────────────────────────────

def validate(data) -> list[str]:
    if not data:
        return ["配置数据为空"]
    errors: list[str] = []
    for key, label in REQUIRED:
        if not str(data.get(key) or "").strip():
            errors.append(f"{label} 不能为空")
    region = str(data.get("region") or "").strip()
    if region and not _REGION_RE.match(region):
        errors.append("BOS 区域格式不正确（只允许小写字母、数字与连字符，如 bj / hkg）")
    return errors


def _fetch_owner(client, ak: str, sk: str, region: str) -> dict:
    """用 BOS ListBuckets 拿账号属主（BCE 账号 ID / 账号名）。

    纯粹是给凭据列表的「附加信息」列补充身份，**失败一律静默**（例如只有 IAM 权限、
    没有 BOS 权限的 AK）—— 判定结果不依赖它。只读，无副作用。
    """
    try:
        resp = _bce_request(client, ak, sk, f"{region}.bcebos.com", "GET", "/")
        if resp.status_code != 200:
            return {}
        owner = (safe_json(resp) or {}).get("owner")
        if not isinstance(owner, dict):
            return {}
        info = {}
        if owner.get("id"):
            info["account_id"] = owner["id"]
        if owner.get("displayName"):
            info["name"] = owner["displayName"]
        return info
    except Exception as exc:  # noqa: BLE001 - 纯属锦上添花，绝不因它影响校验
        log_debug(f"百度智能云：取账号属主失败（{type(exc).__name__}），忽略")
        return {}


@guarded
def verify(data) -> VerifyResult:
    """只读校验：IAM 列举子用户（``GET https://iam.bj.baidubce.com/v1/user``）。

    服务端先校验凭证再路由，所以这个探针能把「凭证不存在」与「凭证存在但没权限」
    分得清清楚楚，且不产生任何副作用。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    if not ak or not sk:
        return VerifyResult(status="error", message="缺少 Access Key ID 或 Secret Access Key")

    method, url, headers, _ = signer.sign_probe(ak, sk, probe="iam")
    with make_client() as client:
        resp = client.request(method, url, headers=headers)
        verdict = signer.interpret(
            resp.status_code, _safe_text(resp), resp.headers.get("content-type", "")
        ).get("verdict")

        if verdict != "valid":
            message = _redact(
                signer.interpret(
                    resp.status_code, _safe_text(resp), resp.headers.get("content-type", "")
                ).get("message") or f"HTTP {resp.status_code}",
                ak, sk,
            )
            # invalid → 明确「凭证不存在/签名校验失败」；restricted → 有效但受限（框架口径）
            if verdict in ("invalid", "restricted"):
                return VerifyResult(status=verdict, message=message)
            return VerifyResult(status="error", message=message)

        # 200：同一份响应体就是子用户列表，顺手拿来丰富展示，不额外发请求
        users = first_list(safe_json(resp) or {}, "users")
        names = [str(u.get("name")) for u in users
                 if isinstance(u, dict) and u.get("name")][:3]
        parts = ["AK/SK 有效（IAM 已通过签名校验）", f"可读 {len(users)} 个 IAM 子用户"]
        if names:
            parts.append("如 " + "、".join(names))

        # 账号身份另走一次只读 ListBuckets（失败不影响结论，见 _fetch_owner）
        region = _field(data, "region") or DEFAULT_REGION
        account_info = _fetch_owner(client, ak, sk, region) or None

    return VerifyResult(status="valid", message="，".join(parts), account_info=account_info)


# ── 权限探测（capabilities）───────────────────────────────

def _probe_one(client, ak: str, sk: str, host: str, path: str, params: Optional[dict],
               json_api: bool) -> str:
    """探一个端点，返回四态之一。探测本身绝不抛出。"""
    try:
        # 注意方法位是位置参数里的第 5 个：漏了它会把 path 当成 method 传给签名器
        resp = _bce_request(client, ak, sk, host, "GET", path, params, json_api=json_api)
        return _classify(resp.status_code, _safe_text(resp), resp.headers.get("content-type", ""))
    except Exception as exc:  # noqa: BLE001 - 探测失败视作「拿不准」
        log_debug(f"百度智能云：探测 {host}{path} 异常（{type(exc).__name__}），按未知处理")
        return _PROBE_UNKNOWN


def capabilities(data: dict) -> dict:
    """只读探测各接口，报告当前这组 AK/SK **真正能用**的操作。

    fail-open 口径：只有接口**明确**回「没权限」（AccessDenied）才把对应操作摘掉；
    网络抖动 / 时间偏差 / 错误体读不出来一律按可用处理。基础身份探针明确 ``bad_cred``
    时直接 ``available=[]``（密钥都被拒了，没什么可提供的）。
    """
    data = data or {}
    ak = _field(data, "access_key_id")
    sk = _field(data, "secret_access_key")
    if not ak or not sk:
        return {"available": [], "note": "缺少 Access Key ID 或 Secret Access Key"}

    region = _field(data, "region") or DEFAULT_REGION

    # (action_id, host, path, params, json_api, 展示名)
    targets = [
        ("list_users", IAM_HOST, IAM_PREFIX + "/user", None, True, "IAM 子用户列表"),
        ("list_groups", IAM_HOST, IAM_PREFIX + "/group", None, True, "IAM 用户组列表"),
        ("list_roles", IAM_HOST, IAM_PREFIX + "/role", None, True, "IAM 角色列表"),
        ("list_policies", IAM_HOST, IAM_PREFIX + "/policy",
         {"policyType": "Custom"}, True, "IAM 权限策略列表"),
        ("list_buckets", f"{region}.bcebos.com", "/", None, False, "BOS 存储桶列表"),
    ]

    results: dict[str, str] = {}
    # client 由**父线程**建好后共享：代理是线程隔离的（vault_proxy_ctx 用 threading.local），
    # worker 里各自 make_client() 拿不到本次凭据的代理。httpx client 本身线程安全。
    with make_client(request_timeout=_PROBE_TIMEOUT) as client:
        with ThreadPoolExecutor(max_workers=min(_SWEEP_WORKERS, len(targets))) as pool:
            futures = {
                pool.submit(_probe_one, client, ak, sk, host, path, params, json_api): action_id
                for action_id, host, path, params, json_api, _ in targets
            }
            for future in as_completed(futures):
                action_id = futures[future]
                try:
                    results[action_id] = future.result()
                except Exception as exc:  # noqa: BLE001 - 单个探测失败不毁整组
                    log_debug(f"百度智能云：{action_id} 探测异常（{type(exc).__name__}）")
                    results[action_id] = _PROBE_UNKNOWN

    # 身份探针（list_users）明确 bad_cred → 整组凭据无效
    if results.get("list_users") == _PROBE_BAD_CRED:
        return {"available": [], "note": "AK/SK 无效或已被禁用"}

    # 全都「拿不准」→ 交给框架 fail-open（available=None 即「未知，全部可用」）
    if all(v == _PROBE_UNKNOWN for v in results.values()):
        return {"available": None, "note": ""}

    labels = {action_id: label for action_id, _, _, _, _, label in targets}
    available, denied = [], []
    for action_id, _, _, _, _, _ in targets:
        if results.get(action_id) == _PROBE_DENIED:
            denied.append(action_id)
        else:
            available.append(action_id)
    # list_objects 要先有桶名才能探，无从判断 → 一律放行（fail-open）
    available.append("list_objects")

    note = ""
    if denied:
        note = "缺少权限（已隐藏）：" + "、".join(labels[a] for a in denied)
    return {"available": available, "note": note}


# ── 操作 ────────────────────────────────────────────────

def _user_row(user: dict) -> dict:
    enabled = user.get("enabled")
    if enabled is None:
        status = ""
    else:
        status = "启用" if enabled else "禁用"
    return {
        "name": user.get("name") or "",
        "id": user.get("id") or "",
        "status": status,
        "description": user.get("description") or "",
        "createTime": user.get("createTime") or "",
        "lastLoginTime": user.get("lastLoginTime") or "",
    }


def _simple_rows(items: list, columns: list) -> list[dict]:
    """把 IAM 的组/角色/策略对象摊成行：只取本表声明过的列，缺列补空串。

    行里的 key 必须与 columns.key 对齐，否则前端取不到值（会渲染成空白单元格）。
    """
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append({col["key"]: item.get(col["key"]) or "" for col in columns})
    return rows


def _run_list_iam(ak: str, sk: str, action_id: str, params: dict) -> dict:
    """四个 IAM 列举操作共用一条通路（同一 host、同一 JSON 约定）。"""
    spec = {
        "list_users": ("/user", None, "users", _USER_COLUMNS, "暂无 IAM 子用户"),
        "list_groups": ("/group", None, "groups", _GROUP_COLUMNS, "暂无 IAM 用户组"),
        "list_roles": ("/role", None, "roles", _ROLE_COLUMNS, "暂无 IAM 角色"),
        "list_policies": ("/policy", None, "policies", _POLICY_COLUMNS, "暂无 IAM 权限策略"),
    }[action_id]
    path, _, list_key, columns, empty_text = spec

    query = None
    if action_id == "list_policies":
        # policyType 只认 System / Custom；不填则官方默认「只返回自定义策略」
        policy_type = _field(params, "policy_type")
        name_filter = _field(params, "name_filter")
        query = {}
        if policy_type in ("System", "Custom"):
            query["policyType"] = policy_type
        if name_filter:
            query["nameFilter"] = name_filter
        query = query or None

    with make_client() as client:
        resp = _bce_request(client, ak, sk, IAM_HOST, "GET", IAM_PREFIX + path, query,
                            json_api=True)
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp, ak, sk, "查询")}

    items = first_list(safe_json(resp) or {}, list_key)
    if action_id == "list_users":
        rows = [_user_row(u) for u in items if isinstance(u, dict)]
    else:
        rows = _simple_rows(items, columns)
    return {
        "ok": True,
        "rows": rows,
        "columns": columns,
        "total": len(rows),
        "empty_text": empty_text,
    }


def _run_list_buckets(ak: str, sk: str, region: str) -> dict:
    """BOS ListBuckets（``GET https://<区域>.bcebos.com/``）。

    注意 hosts 是**区域无关**的：Service 级枚举一次就返回该账号全部区域的桶（上限 100），
    每行自带 ``location``，所以不必逐区域枚举 —— 行内「浏览」直接用这一列下钻即可。
    """
    host = f"{region}.bcebos.com"
    with make_client() as client:
        resp = _bce_request(client, ak, sk, host, "GET", "/")
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp, ak, sk, "查询")}

    payload = safe_json(resp) or {}
    buckets = payload.get("buckets")
    if not isinstance(buckets, list):
        buckets = first_list(payload, "buckets")

    rows = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        rows.append({
            "name": bucket.get("name") or "",
            "location": bucket.get("location") or "",
            "creationDate": bucket.get("creationDate") or "",
            "enableMultiAz": "是" if bucket.get("enableMultiAz") else "否",
        })
    return {
        "ok": True,
        "rows": rows,
        "columns": _BUCKET_COLUMNS,
        "row_actions": [_BUCKET_ROW_ACTION],
        "total": len(rows),
        "empty_text": f"暂无 BOS 存储桶（服务域名 {host}）",
    }


def _run_list_objects(ak: str, sk: str, params: dict, default_region: str) -> dict:
    """BOS 列举对象（``?prefix=&delimiter=/&maxKeys=``），文件与文件夹分行。

    ``delimiter=/`` 让服务端把「子目录」汇总进 ``commonPrefixes``，一次就能列出
    「当前目录」的完整内容；``isTruncated`` / ``nextMarker`` 表达还有更多。
    """
    bucket = _field(params, "bucket")
    if not bucket:
        return {"ok": False, "error": "请先指定存储桶（可从「BOS 存储桶列表」行内进入）"}
    if not _BUCKET_NAME_RE.match(bucket):
        return {"ok": False,
                "error": "存储桶名不合法（BOS 只允许小写字母、数字与连字符，长度 3–63）"}
    region = _field(params, "region") or default_region
    if not _REGION_RE.match(region):
        return {"ok": False, "error": "区域格式不正确（如 bj / gz / hkg）"}
    prefix = _field(params, "prefix")

    query = {"maxKeys": str(_PAGE_SIZE), "delimiter": "/"}
    if prefix:
        query["prefix"] = prefix

    host = f"{bucket}.{region}.bcebos.com"
    with make_client() as client:
        resp = _bce_request(client, ak, sk, host, "GET", "/", query)
    if resp.status_code != 200:
        return {"ok": False, "error": _failure(resp, ak, sk, "查询")}

    payload = safe_json(resp) or {}
    rows: list[dict] = []

    # 文件夹行（commonPrefixes）：带 folderPath 供行内「进入」下钻
    for node in payload.get("commonPrefixes") or []:
        if not isinstance(node, dict):
            continue
        child = node.get("prefix") or ""
        if not child:
            continue
        rows.append({
            "bucket": bucket,
            "region": region,
            "key": child,
            "type": "文件夹",
            "size": "",
            "lastModified": "",
            "folderPath": child,
        })

    # 文件行：目录占位对象（key 以 / 结尾）已由 commonPrefixes 表达，跳过免得重复
    for node in payload.get("contents") or []:
        if not isinstance(node, dict):
            continue
        key = node.get("key") or ""
        if not key or key.endswith("/"):
            continue
        rows.append({
            "bucket": bucket,
            "region": region,
            "key": key,
            "type": "文件",
            "size": node.get("size") if node.get("size") is not None else "",
            "lastModified": node.get("lastModified") or "",
            "fileKey": key,
        })

    where = prefix or "桶根目录"
    return {
        "ok": True,
        "rows": rows,
        "columns": _OBJECT_COLUMNS,
        "row_actions": _OBJECT_ROW_ACTIONS,
        "total": len(rows),
        "has_more": bool(payload.get("isTruncated")),
        "empty_text": f"该前缀下暂无对象（{bucket} / {where}，仅显示前 {_PAGE_SIZE} 个）",
    }


def actions(data: dict) -> list[dict]:
    """声明可用操作。凭据不完整时返回空列表（前端不显示「操作」入口）。"""
    data = data or {}
    if not (_field(data, "access_key_id") and _field(data, "secret_access_key")):
        return []
    region = _field(data, "region") or DEFAULT_REGION
    return [
        {
            "id": "list_users",
            "name": "IAM 子用户列表",
            "kind": "list",
            "group": "身份与权限",
            "order": 10,
            "description": "IAM 列举子用户（GET /v1/user，只读）。当前 AK 所属主账号读不到 IAM 会返回 AccessDenied",
            "columns": _USER_COLUMNS,
            "empty_text": "暂无 IAM 子用户",
        },
        {
            "id": "list_groups",
            "name": "IAM 用户组列表",
            "kind": "list",
            "group": "身份与权限",
            "order": 20,
            "description": "IAM 列举用户组（GET /v1/group，只读）",
            "columns": _GROUP_COLUMNS,
            "empty_text": "暂无 IAM 用户组",
        },
        {
            "id": "list_roles",
            "name": "IAM 角色列表",
            "kind": "list",
            "group": "身份与权限",
            "order": 30,
            "description": "IAM 列举角色（GET /v1/role，只读）",
            "columns": _ROLE_COLUMNS,
            "empty_text": "暂无 IAM 角色",
        },
        {
            "id": "list_policies",
            "name": "IAM 权限策略列表",
            "kind": "list",
            "group": "身份与权限",
            "order": 40,
            "description": "IAM 列举权限策略（GET /v1/policy，只读）。官方语义：不指定类型时服务端只返回自定义策略，要看系统策略请把类型切成 System",
            "columns": _POLICY_COLUMNS,
            "fields": _IAM_FIELDS,
            "submit": "查询",
            "empty_text": "暂无 IAM 权限策略",
        },
        {
            "id": "list_buckets",
            "name": "BOS 存储桶列表",
            "kind": "list",
            "group": "对象存储",
            "order": 50,
            "description": (
                f"BOS 列举存储桶（GET https://<区域>.bcebos.com/，只读，区域默认 {region}）。"
                "服务级域名一次返回该账号**全部区域**的桶（上限 100），无需逐区域枚举；"
                "行内「浏览」按该桶自己的区域下钻到对象列表"
            ),
            "columns": _BUCKET_COLUMNS,
            "row_actions": [_BUCKET_ROW_ACTION],
            "empty_text": f"暂无 BOS 存储桶（服务域名 {region}.bcebos.com）",
        },
        {
            "id": "list_objects",
            "name": "BOS 对象浏览",
            "kind": "list",
            "group": "对象存储",
            "order": 60,
            "description": (
                "BOS 列举对象（ListObjects + delimiter=/，只读）。请从「BOS 存储桶列表」"
                "行内「浏览」进入，区域会自动填成该桶所在区域；手填时区域必须与桶一致，"
                "否则会报桶不存在"
            ),
            "columns": _OBJECT_COLUMNS,
            "fields": _OBJECT_FIELDS,
            "row_actions": _OBJECT_ROW_ACTIONS,
            "submit": "查询",
            "empty_text": f"该前缀下暂无对象（仅显示前 {_PAGE_SIZE} 个）",
        },
    ]


def run_action(action_id: str, params: dict, data: dict, progress=None, should_cancel=None) -> dict:
    """执行操作（全部只读，绝不产生远端变更）。

    ``progress`` / ``should_cancel`` 是下载类操作的钩子；本插件的操作都是「一次列举、
    一次返回」，用不上，保留形参只为与框架契约一致。
    """
    data = data or {}
    params = params or {}
    try:
        ak = _field(data, "access_key_id")
        sk = _field(data, "secret_access_key")
        if not ak or not sk:
            return {"ok": False, "error": "Access Key ID 或 Secret Access Key 为空"}
        region = _field(data, "region") or DEFAULT_REGION

        if action_id in ("list_users", "list_groups", "list_roles", "list_policies"):
            return _run_list_iam(ak, sk, action_id, params)
        if action_id == "list_buckets":
            return _run_list_buckets(ak, sk, region)
        if action_id == "list_objects":
            return _run_list_objects(ak, sk, params, region)
        return {"ok": False, "error": "未知操作"}
    except Exception as exc:  # noqa: BLE001 - 操作边界兜底，绝不抛出
        # 不回显 str(exc)：httpx 异常文本含完整请求 URL，会把 AK/SK 泄进界面与日志
        return {"ok": False, "error": f"执行异常：{type(exc).__name__}"}


def get_display_summary(data) -> str:
    data = data or {}
    detail = str(data.get(DISPLAY_FIELD) or "").strip()
    return f"{NAME} | {detail}" if detail else NAME
