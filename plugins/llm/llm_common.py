"""LLM 类别共享模块：**最底层通用工具** + 子插件原型接口。

只提供与厂商、协议都无关的最底层能力：

- 结果类型 `VerifyResult`
- 代理读取 `proxy`、HTTP 客户端 `make_client`
- 响应解析 `safe_json` / `body_snippet`
- 异常兜底 `guarded`、日志 `log`
- 超时 `TIMEOUT`（默认值）/ `timeout()`（实际生效值，读 `VAULT_TIMEOUT`）
- 欠费探测 `make_quota_probe()`（通用实现）与 `probe_requested()` / `probe_model()` /
  `quota_ok()` / `quota_insufficient()`（非通用鉴权的插件自建 `quota_probe` 时复用）
- 常量 `_BODY_LIMIT`

协议级工具（探针、简单对话等跨厂商完全相同的能力）放在**独立的工具模块**
`plugins/llm/llm_protocols.py`，由需要的子插件按需 `from llm_protocols import ...`。

各厂商的默认地址、模型名、余额端点与解析等**专属逻辑**一律写在
`plugins/llm/<厂商>/plugin.py` 内（厂商之间允许重复，刻意为之）——
新增厂商只需照抄同级子插件，无需修改本模块或协议工具模块。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class VerifyResult:
    """插件验证结果（独立定义，不依赖 backend）"""

    status: str  # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


# 请求超时（秒）—— 模块默认值；实际生效值走 timeout()
TIMEOUT = 20.0


def timeout() -> float:
    """实际使用的超时秒数：``VAULT_TIMEOUT``（应用设置写入）> 模块默认 ``TIMEOUT``。

    后端「批量验证」对话框里的超时设置经环境变量下发（插件是独立模块，不允许
    import backend），每次发请求时读取——改完即时生效，无需重启。变量缺失或非法时
    回退到模块默认值。
    """
    raw = os.environ.get("VAULT_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return TIMEOUT

# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


_logger = logging.getLogger("plugin.llm")


def log(message: str) -> None:
    """插件日志 → 应用统一日志（根 logger 落盘到 data/logs/credential_vault.log）。

    不要改回 print(..., file=sys.stderr)：打包成 GUI 程序后没有 stdio，
    输出会被静默丢弃（在 windowed 模式下写 stderr 甚至直接抛异常），
    结果就是插件的告警与异常在系统日志里完全不可见。
    """
    _logger.info("%s", message)


# ── 请求封装 ─────────────────────────────────────────────

def proxy() -> Optional[str]:
    """本次请求使用的代理。

    优先取**框架按调用注入**的覆盖值（``vault_proxy_ctx``）——批量验证时每条凭据各一份、
    线程隔离，所以「不同凭据走不同代理」不会互相踩；没有覆盖时回退环境变量：
    ``VAULT_PROXY``（应用设置写入）> 标准代理变量。

    覆盖值为 ``DIRECT`` 表示该凭据选了「直连」，此时**压过**上面所有环境变量。
    插件脱离主程序单独运行时 ``vault_proxy_ctx`` 不在 ``sys.modules`` 里，直接走环境变量。
    """
    ctx = sys.modules.get("vault_proxy_ctx")
    if ctx is not None:
        value = ctx.current()
        if value is not ctx.UNSET:
            return None if value is ctx.DIRECT else value
    for key in ("VAULT_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def make_client(follow_redirects: bool = True) -> httpx.Client:
    return httpx.Client(timeout=timeout(), follow_redirects=follow_redirects, proxy=proxy())


def safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(response: httpx.Response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


# ── 金额合理性 ───────────────────────────────────────────

# 金额上限（美元/人民币）：正规 LLM 账户的余额或配额不会到千万量级。
# one-api / new-api 类中转站在「不限额」时会把硬限额填成 99999999.99 这类哨兵值，
# 拿它去减消耗就会得出 99999996.47 这种假余额，所以任何要展示的金额都先过一遍这个闸。
AMOUNT_LIMIT = 1e7


def plausible_amount(value) -> bool:
    """金额是否可信到可以展示（有限数值，且绝对值不超过 ``AMOUNT_LIMIT``）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(num) and abs(num) < AMOUNT_LIMIT


def guarded(fn):
    """装饰器：兜底 verify/validate 未预期异常 → error 结果，保证不抛出。

    异常消息只暴露异常类型名，避免把 URL（部分厂商密钥在 query 中）
    或响应正文带进错误提示。
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except httpx.TimeoutException:
            return VerifyResult(status="error", message=f"请求超时（{int(timeout())} 秒）")
        except httpx.HTTPError as exc:
            log(f"verify 网络异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"网络请求失败（{type(exc).__name__}）")
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常（{type(exc).__name__}）")

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


# ── 欠费探测（余额拿不到时的可选项） ─────────────────────
#
# 契约：子插件可导出 ``quota_probe(data) -> Optional[dict]``，后端在「验证通过且没拿到
# 余额/使用率」时调用它（见 backend/plugin_manager._apply_quota_probe）。返回值：
#   None                          → 未开启 / 无结论 / 实测无异常，后端什么都不改
#   {"account_info": {...}}       → 补充展示字段（用 quota_ok()）
#   {"message": "..."}            → 替换状态说明（用 quota_insufficient()）
#
# 绝大多数插件用 ``quota_probe = make_quota_probe(DEFAULT_BASE_URL, "<对话协议>")`` 一行接入；
# **鉴权非通用格式的插件**（如百度文心要先拿 API_KEY|SECRET_KEY 换 access_token、聊天端点
# 还带模型后缀）在自己的 plugin.py 里实现 quota_probe，复用下面这些读取/结论辅助函数即可。

def _truthy(value) -> bool:
    """凭据里的开关字段是否为真（兼容布尔与字符串形式）。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def probe_requested(data: dict) -> bool:
    """该凭据是否开启了「欠费探测」（凭据字段 ``probe_chat``，默认关闭）。"""
    return _truthy((data or {}).get("probe_chat"))


def probe_model(data: dict) -> str:
    """该凭据指定的探测模型（凭据字段 ``probe_model``）；空串 = 自动挑一个便宜的。"""
    return str((data or {}).get("probe_model") or "").strip()


def quota_ok() -> dict:
    """探测结论：实测可用 → 附加信息补一项「实测 可用」。"""
    return {"account_info": {"chat_probe": "可用"}}


def quota_insufficient() -> dict:
    """探测结论：实测欠费 → 替换状态说明（``status`` 仍为 valid，欠费 ≠ 凭据失效）。"""
    return {"message": "凭证有效，但账户欠费或无可用额度（已用最小对话实测）"}


def make_quota_probe(default_base_url, protocol="openai"):
    """生成子插件的 ``quota_probe(data)`` —— 通用（OpenAI / Anthropic / Gemini 协议）实现。

    ``default_base_url`` 与 ``protocol`` 可以是常量，也可以是 ``fn(data) -> 值`` 的**可调用对象**
    ——同一插件支持多种接口格式时（如智谱 GLM 同时提供 OpenAI 与 Anthropic 端点）用它按凭据取。

    开关与模型取自**凭据自身**的字段（``probe_chat`` / ``probe_model``），逐条凭据独立开启，
    不涉及全局设置。真正发请求的是 ``llm_protocols.quota_chat_probe``（延迟导入，避免模块成环）。
    非通用鉴权的插件请自行实现 ``quota_probe``，复用 ``probe_requested`` / ``probe_model`` /
    ``quota_ok`` / ``quota_insufficient`` 即可。
    """
    def quota_probe(data: dict) -> Optional[dict]:
        data = data or {}
        if not probe_requested(data):
            return None
        default = default_base_url(data) if callable(default_base_url) else default_base_url
        proto = protocol(data) if callable(protocol) else protocol
        base_url = (data.get("base_url") or "").strip() or default
        api_key = (data.get("api_key") or "").strip()
        if not base_url or not api_key:
            return None
        from llm_protocols import quota_chat_probe
        return quota_chat_probe(base_url, api_key, proto, probe_model(data))

    return quota_probe


# ── 子插件原型接口（子插件应实现；这里只声明契约，运行时不调用本模块的实现）──

def validate(data: dict) -> list:
    """子插件实现：返回配置错误列表（空列表 = 通过）。"""
    raise NotImplementedError


def verify(data: dict) -> VerifyResult:
    """子插件实现：验证凭据，返回 VerifyResult（valid / invalid / error）。"""
    raise NotImplementedError


def get_display_summary(data: dict) -> str:
    """子插件实现：凭据列表中的简短展示文本。"""
    return ""


def actions(data: dict) -> list:
    """子插件可选实现：声明可用操作（如简单对话）；返回 [] 表示无操作。"""
    return []


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """子插件可选实现：执行操作，返回 {"ok": bool, "output"/"error": str}。"""
    return {"ok": False, "error": "未实现"}


def quota_probe(data: dict) -> Optional[dict]:
    """子插件可选实现：余额拿不到时发一次最小对话请求判断欠费。

    通用协议用 ``quota_probe = make_quota_probe(DEFAULT_BASE_URL, "<对话协议>")`` 一行接入；
    **鉴权/端点非通用格式的插件**（如百度文心）按上方契约自行实现，复用
    ``probe_requested`` / ``probe_model`` / ``quota_ok`` / ``quota_insufficient``。
    未导出该函数时后端跳过此检查。
    """
    return None
# ── 写操作/列表类操作的通用辅助（新增插件时直接复用，别自己重造） ──

def download_to_file(response: httpx.Response, path: str) -> tuple[bool, str]:
    """把**已打开的流式响应**写进 ``path``，返回 ``(ok, message)``。

    配合「另存为」的用法（路径由前端选好、经 ``params`` 传进来）::

        with make_client() as client:
            with client.stream("GET", url, headers=headers) as resp:
                ok, message = download_to_file(resp, params.get("path"))

    两个刻意的设计：

    - **原子落盘**：先写同目录的 ``.part`` 再 ``os.replace``。中途断流只会留下一个 ``.part``，
      不会出现「名字正常、内容截断」的半成品被误当成下载成功。
    - **非 200 直接失败、不碰磁盘**：调用方拿到 False 就能如实报错。
    """
    if response.status_code != 200:
        return False, f"下载失败（HTTP {response.status_code}）"
    target = str(path or "").strip()
    if not target:
        return False, "未指定保存路径"
    tmp = target + ".part"
    try:
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes(65536):
                handle.write(chunk)
        os.replace(tmp, target)      # 同目录内的替换是原子的
    except Exception as exc:  # noqa: BLE001 - 插件边界兜底
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"写入失败：{type(exc).__name__}"
    return True, target


def first_list(payload, *keys) -> list:
    """从各种上游 JSON 信封里捞出**第一个列表**。

    云厂商与中转站对「列表」的字段名与嵌套层次都极不统一
    （``{"items": [...]}`` / ``{"data": {"list": [...]}}`` / ``{"Response": {"ZoneList": [...]}}`` …），
    写插件时逐个猜既累又容易漏。用法::

        rows = first_list(payload, "items", "list")            # 先按已知字段名找
        rows = first_list(payload)                             # 不行就让它自己找

    查找顺序：**先递归按** ``keys`` **找**（限 3 层，所以 ``{"data": {"items": []}}`` 也能命中），
    都没有时再退一步找「第一个 list 值」。**拿不准时请显式传 keys**，否则可能捞到
    ``{"errors": [...]}`` 这类无关列表。
    """
    def _by_keys(node, depth):
        if depth > 3 or not isinstance(node, dict):
            return None
        for key in keys:
            value = node.get(key)
            if isinstance(value, list):
                return value
        for value in node.values():
            if isinstance(value, dict):
                found = _by_keys(value, depth + 1)
                if found:
                    return found
        return None

    def _any_list(node, depth):
        if depth > 3:
            return None
        if isinstance(node, list):
            return node
        if isinstance(node, dict):
            for value in node.values():
                found = _any_list(value, depth + 1)
                if found:
                    return found
        return None

    if isinstance(payload, list):
        return payload
    return _by_keys(payload, 0) or _any_list(payload, 0) or []
