"""UCloud 只读探针签名实现。

经「官方 SDK 源码」与「官方文档」双向校准，不自造算法。

逐字源码摘录（含 ``文件:行``）见 ``docs/SIGNER_CALIBRATION.md`` 第 3 节。

证据来源
--------
1. 官方 SDK ``ucloud/ucloud-sdk-python3`` @ commit ``cc58bf1`` (v0.11.145, 2026-10-09)

   - ``ucloud/core/auth/_cfg.py``            —— ``verify_ac()`` 签名算法本体
   - ``ucloud/core/client/_client.py``       —— ``_build_http_request()`` 请求构造
   - ``ucloud/core/typesystem/encoder.py``   —— ``encode_value()`` 值编码(无 URL 转义)
   - ``tests/test_unit/test_core/test_auth.py`` —— 官方固定测试向量

2. 官方文档仓库 ``UCloudDoc-Team/api`` (docs.ucloud.cn 的源)

   - ``summary/public.md``   —— 公共参数 / 公共响应 / **完整错误码表**
   - ``summary/signature``   —— 签名算法(hex / 无 HTTP 转义 / 末尾拼 PrivateKey)

关键结论(已用官方测试向量逐条自证, 见文件末 ``_self_test``)
----------------------------------------------------------
* 摘要算法: **SHA1**, 输出 **hex**(40 位小写十六进制), **不是 base64**。
  ``_cfg.py`` -> ``hash_new = hashlib.sha1(); ... return hash_new.hexdigest()``
* 拼接串 **不做任何 URL/HTTP 转义**。值一律 ``str(v)`` 后直接拼接。
  ``encoder.py`` -> ``encode_value()`` 仅对 bool/float 做特判, 其余 ``return str(v)``
* **键值之间无分隔符**, 形如 ``<key><value><key><value>...``。
* 参数按**键名升序**排序；``PublicKey`` **参与签名**；``Signature`` 不参与。
* 串末尾**直接追加 PrivateKey, 无任何分隔符**。
  ``_cfg.py`` -> ``simplified += private_key``
* 请求方式: 官方文档明确支持 ``GET``/``POST`` × ``application/json``/``x-www-form-urlencoded``
  四种组合(``summary/public.md``「UCloud API 支持 GET / POST 两种方法, 支持
  application/json 和 application/x-www-form-urlencoded 两种请求方式」)。
  本模块用 **POST + JSON body**(官方文档 curl 示例即为此形式)。
  注: 官方 *Python SDK* 默认走 ``POST + x-www-form-urlencoded``(``_client.py`` 里
  ``data=payload``), 二者服务端都接受。

参考文档:
    https://docs.ucloud.cn/api/summary/signature      签名算法
    https://docs.ucloud.cn/api/summary/public         公共参数/响应/错误码
    https://github.com/ucloud/ucloud-sdk-python3      官方 Python SDK
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict

__all__ = ["sign_probe", "interpret", "sign_params", "API_ENDPOINT", "DEFAULT_ACTION"]

#: UCloud 公有云通用 API 接入点(官方 SDK Config 默认 base_url)
API_ENDPOINT = "https://api.ucloud.cn"

#: 默认探针 Action。
#:
#: ⚠️ 注意: 任务书里建议的 ``ListRegions`` **并非真实存在的 UCloud Action**,
#: 对合法密钥也会返回 ``RetCode=161``(Action 不存在), 从而把有效凭据误判为异常。
#: 经官方 SDK 核实, 真实且最合适的只读探针是 uaccount 服务的 ``GetRegion``
#: (``ucloud/services/uaccount/client.py::get_region``), 语义为
#: 「获取用户在各数据中心的权限等信息」, **无需任何参数**, 只读、零副作用。
DEFAULT_ACTION = "GetRegion"

#: 与官方 SDK 同形的 User-Agent(用于混入正常流量)
_USER_AGENT = "Python/3.11 Python-SDK/0.11.145"


def _encode_value(v) -> str:
    """复刻官方 ``ucloud/core/typesystem/encoder.py::encode_value`` 的值编码。

    仅 bool/float 有特判, 其余一律 ``str(v)``; **不做 URL 转义**。
    """
    if isinstance(v, bool):  # 必须放在 int 判断之前
        return "true" if v else "false"
    if isinstance(v, float):
        # 官方注释: api gateway 会把 float 当 int 解, 整数值只留整数部分
        return str(int(v)) if v % 1 == 0 else str(v)
    return str(v)


def sign_params(private_key: str, params: dict) -> str:
    """对参数求 UCloud 签名, 返回 40 位小写 hex。

    对应官方 ``ucloud/core/auth/_cfg.py``::

        params = OrderedDict(sorted(params.items(), key=lambda item: item[0]))
        simplified = ""
        for key, value in params.items():
            simplified += str(key) + encoder.encode_value(value)
        simplified += private_key
        hash_new = hashlib.sha1()
        hash_new.update(simplified.encode("utf-8"))
        return hash_new.hexdigest()

    :param private_key: UCloud PrivateKey
    :param params: 参与签名的参数(**必须已包含 PublicKey**, 且不得包含 Signature)
    """
    ordered = OrderedDict(sorted(params.items(), key=lambda item: item[0]))
    simplified = ""
    for key, value in ordered.items():
        simplified += str(key) + _encode_value(value)
    simplified += private_key
    return hashlib.sha1(simplified.encode("utf-8")).hexdigest()


def sign_probe(
    public_key: str,
    private_key: str,
    *,
    action: str = DEFAULT_ACTION,
    endpoint: str = API_ENDPOINT,
    timestamp_ms: int | None = None,
):
    """构造一个 UCloud 只读探针请求。

    :return: ``(method, url, headers, body)`` —— body 为 JSON 文本(str)
    """
    # 1) 组装参与签名的参数: Action + PublicKey(PublicKey 必须参与签名)
    params: dict = {
        "Action": action,
        "PublicKey": public_key,
    }

    # 2) 求签名(仅对上述参数, 不含 Signature 自身)
    params["Signature"] = sign_params(private_key, params)

    # 3) 序列化 body。签名与 body 顺序无关(内部已排序), 这里按键升序输出以贴近官方示例
    body = json.dumps(
        OrderedDict(sorted(params.items(), key=lambda item: item[0])),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        # 官方 SDK 会带此毫秒时间戳头(_client.py::_build_http_request)
        "U-Timestamp-Ms": str(timestamp_ms if timestamp_ms is not None else int(round(time.time() * 1000))),
    }

    return "POST", endpoint, headers, body


# --------------------------------------------------------------------------- #
# RetCode 归类
# --------------------------------------------------------------------------- #
#
# 权威来源(两份官方表, 分属不同产品线):
#
# [A] 公有云 api.ucloud.cn —— UCloudDoc-Team/api ``summary/public.md`` 「错误码说明」
#     110 响应超时 / 130 鉴权服务异常 / 150 服务不可用 / 153 限流 /
#     160 缺 Action / 161 Action 不存在 / 170 缺少签名 / 171 签名错误 /
#     172 用户不存在 / 174 Token 不存在 / 292 项目不存在 /
#     293 可用区权限错误 / 294 访问 IP 被拒绝 / 295 安全锁验证失败 /
#     299 IAM 权限校验未通过
#
# [B] 私有云 UCloudStack v2.x —— UCloudDoc-Team/UCloudStack_v2.x ``apiretcode/README.md``
#     104 缺少签名 / 109 签名不合法 / 111 必须管理员调用 / 112 IP 不在白名单 /
#     114 用户无此接口权限 / 14001/14002/14007 集群权限 …
#
# 任务书给的「171/109=签名错误, 104=缺签名」实际上 **混用了 A、B 两套编号**:
#   171 属公有云; 104/109 属私有云 UCloudStack。二者均为官方文档, 只是产品线不同。
#   对 api.ucloud.cn 探针, 真正会命中的是 170/171; 104/109 一并纳入以防跨产品线复用。
#
# 归类策略:
#   * HTTP 200 且 RetCode == 0            -> valid
#   * 签名/身份类 RetCode                 -> invalid   (密钥不被接受)
#   * 权限/白名单/IAM 类 RetCode          -> restricted(签名通过但无权限, 仍证明凭据有效)
#   * 其余非 0 / HTTP 非 200 / 解析失败   -> error

#: 签名错误或身份不存在 —— 密钥材料本身不被接受
_INVALID_CODES = {
    104,  # 缺少签名(UCloudStack)
    109,  # 签名不合法(UCloudStack)
    170,  # 缺少签名(公有云)
    171,  # 签名错误(公有云)
    172,  # 用户不存在(公有云)—— PublicKey 未映射到任何用户
}

#: 权限/访问控制类 —— 说明签名已通过, 只是无权访问
_RESTRICTED_CODES = {
    111,    # 没有权限, 必须管理员用户调用(UCloudStack)
    112,    # 访问 IP 不在租户白名单内(UCloudStack)
    114,    # 用户无此接口权限(UCloudStack)
    293,    # 可用区权限错误(公有云)
    294,    # 访问 IP 被拒绝(公有云)
    295,    # 安全锁验证失败(公有云)
    299,    # IAM 权限校验未通过(公有云)
    14001,  # 没有计算集群权限(UCloudStack)
    14002,  # 没有存储集群权限(UCloudStack)
    14007,  # 用户对集群资源没有权限(UCloudStack)
}

#: 便于人工核对的中文说明
_CODE_DESC = {
    0: "成功",
    104: "缺少签名",
    109: "签名不合法",
    110: "API 响应超时",
    111: "必须管理员用户调用",
    112: "访问 IP 不在白名单",
    114: "用户无此接口权限",
    130: "API 网关或认证鉴权服务异常",
    150: "服务暂不可用",
    153: "请求被限流",
    160: "缺少关键参数 Action",
    161: "Action 不存在",
    170: "缺少签名",
    171: "签名错误",
    172: "用户不存在",
    174: "Token 不存在",
    292: "项目不存在",
    293: "可用区权限错误",
    294: "访问 IP 被拒绝",
    295: "安全锁验证失败",
    299: "IAM 权限校验未通过",
    14001: "没有计算集群权限",
    14002: "没有存储集群权限",
    14007: "用户对集群资源没有权限",
}


def interpret(status_code: int, body_text) -> dict:
    """根据 HTTP 状态码与响应体判定凭据状态。

    :return: ``{"verdict": "valid"|"invalid"|"restricted"|"error", "message": str}``
    """
    if isinstance(body_text, (bytes, bytearray)):
        try:
            body_text = body_text.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode(errors=replace) 不会抛
            body_text = str(body_text)

    text = (body_text or "").strip()
    if not text:
        return {
            "verdict": "error",
            "message": f"HTTP {status_code} 返回空响应体",
        }

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {
            "verdict": "error",
            "message": f"HTTP {status_code} 响应非 JSON, 无法解析: {text[:200]}",
        }

    if not isinstance(data, dict) or "RetCode" not in data:
        return {
            "verdict": "error",
            "message": f"HTTP {status_code} 响应缺少 RetCode 字段: {text[:200]}",
        }

    try:
        ret = int(data["RetCode"])
    except (TypeError, ValueError):
        return {
            "verdict": "error",
            "message": f"RetCode 非整数: {data.get('RetCode')!r}",
        }

    msg = str(data.get("Message", "") or "").strip()
    desc = _CODE_DESC.get(ret, "未知错误码")

    # 签名通过且业务成功, 必须 HTTP 200; 若 RetCode=0 但 HTTP 异常, 视为异常
    if ret == 0 and status_code == 200:
        return {
            "verdict": "valid",
            "message": f"凭据有效 (RetCode=0, {desc})",
        }
    if ret == 0 and status_code != 200:
        return {
            "verdict": "error",
            "message": f"RetCode=0 但 HTTP 状态异常 ({status_code})",
        }

    if ret in _INVALID_CODES:
        return {
            "verdict": "invalid",
            "message": f"凭据无效 (RetCode={ret}, {desc})" + (f": {msg}" if msg else ""),
        }

    if ret in _RESTRICTED_CODES:
        return {
            "verdict": "restricted",
            "message": f"凭据有效但权限受限 (RetCode={ret}, {desc})" + (f": {msg}" if msg else ""),
        }

    return {
        "verdict": "error",
        "message": f"HTTP {status_code} RetCode={ret} ({desc})" + (f": {msg}" if msg else ""),
    }


# --------------------------------------------------------------------------- #
# 自测: 用官方固定向量验证算法
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    cases = []

    # 向量 1: SDK docstring  verify_ac("my_private_key", {"foo": "bar"})
    # 来源 ucloud/core/auth/_cfg.py 模块 docstring
    cases.append((
        "SDK docstring verify_ac",
        sign_params("my_private_key", {"foo": "bar"}),
        "634edc1bb957c0d65e5ab5494cf3b7784fbc87af",
    ))

    # 向量 2: SDK docstring  Credential('my_public_key','my_private_key').verify_ac({"foo":"bar"})
    cases.append((
        "SDK docstring Credential.verify_ac",
        sign_params("my_private_key", {"foo": "bar", "PublicKey": "my_public_key"}),
        "d4411ab30953fb0bbcb1e7313081f05e4e91a394",
    ))

    # 向量 3: 官方真实单测 tests/test_unit/test_core/test_auth.py
    cases.append((
        "SDK unit test test_verify_ac",
        sign_params(
            "46f09bb9fab4f12dfc160dae12273d5332b5debe",
            {
                "Action": "CreateUHostInstance",
                "CPU": 2,
                "ChargeType": "Month",
                "DiskSpace": 10,
                "ImageId": "f43736e1-65a5-4bea-ad2e-8a46e18883c2",
                "LoginMode": "Password",
                "Memory": 2048,
                "Name": "Host01",
                "Password": "VUNsb3VkLmNu",
                "PublicKey": "ucloudsomeone@example.com1296235120854146120",
                "Quantity": 1,
                "Region": "cn-bj2",
                "Zone": "cn-bj2-04",
            },
        ),
        "4f9ef5df2abab2c6fccd1e9515cb7e2df8c6bb65",
    ))

    # 向量 4: 官方文档 summary/public.md 的 curl 示例(证明 PublicKey 参与签名)
    cases.append((
        "官方文档 public.md curl 示例",
        sign_params(
            "46f09bb9fab4f12dfc160dae12273d5332b5debe",
            {
                "Action": "DescribeUHostInstance",
                "Limit": 10,
                "PublicKey": "ucloudsomeone@example.com1296235120854146120",
                "Region": "cn-bj2",
            },
        ),
        "cba5cf5ec4d4233d206b1b54951e3787350a642f",
    ))

    ok = True
    for name, got, want in cases:
        good = got == want
        ok = ok and good
        print(f"[{'PASS' if good else 'FAIL'}] {name}\n       got ={got}\n       want={want}")
    print("ALL PASS" if ok else "SOME FAILED")
    return ok


if __name__ == "__main__":
    import sys

    sys.exit(0 if _self_test() else 1)
