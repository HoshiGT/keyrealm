"""插件 SDK：框架向插件暴露的**唯一**接口层。

框架把「与具体服务无关的通用能力」全部收在这里，插件只需 `import` 本模块 + 写自己的
功能逻辑，无需在每个类别目录下各留一份逐字节相同的工具代码。

提供的能力：

- 结果类型 `VerifyResult`
- 日志 `log` / `log_debug`
- 代理读取 `proxy`、HTTP 客户端 `make_client`
- TLS 版本策略 `tls_version()`（`auto` / `1.0` / `1.1` / `1.2` / `1.3`，`make_client` 自动应用）
- 响应解析 `safe_json` / `body_snippet`
- 异常兜底 `guarded`
- 超时 `TIMEOUT`（默认值）/ `timeout()`（实际生效值，读 `VAULT_TIMEOUT`）
- 写/列表类操作的通用辅助 `download_to_file` / `first_list`
- 凭据值脱敏 `mask_secret`（写日志/提示时用，**不要自己切字符串**）

用法::

    from backend.plugin_sdk import VerifyResult, guarded, make_client, safe_json

    @guarded
    def verify(data):
        with make_client() as client:
            resp = client.get("https://api.example.com/me",
                              headers={"Authorization": f"Bearer {data['api_key']}"})
        ...

**插件只允许 `import backend.plugin_sdk`，不得 import backend 的其它模块** —— 本模块是
框架与插件之间唯一被承诺稳定的边界，backend 内部实现可以自由重构而不影响插件。

本模块是**叶子模块**：只依赖标准库与 httpx，不 import 任何 backend 模块。因此它
自包含、可单独拷出去用，也不会与 backend 内部形成循环依赖。

插件需实现的接口（标「可选」的未实现即视为不支持，框架侧在 `backend/plugin_base.py`
里统一做鸭子类型适配，签名对不上也不会报错）：

============================== =================================================
`verify(data) -> VerifyResult`  **必选**：验证凭据 → valid / invalid / error
`validate(data) -> list`        可选：字段错误列表（空 = 通过）
`get_display_summary(data)`     可选：凭据列表中的简短展示文本
`actions(data) -> list`         可选：声明可用操作
`run_action(id, params, data)`  可选：执行操作 → `{"ok", "output", "error"}`
`capabilities(data) -> dict`    可选：报告当前凭据**真正能用**的操作（fail-open）
`list_models(data, force)`      可选：可用模型名列表（force=用户手动刷新，穿透清单缓存）
`stream_action(...)`            可选：流式执行操作（回报进度、支持取消）
`quota_probe(data) -> dict`     可选：余额拿不到时的欠费探测
============================== =================================================
"""
from __future__ import annotations

import logging
import os
import ssl
import sys
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class VerifyResult:
    """插件验证结果（框架与插件共用的权威定义）"""

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


def timeout(fallback: float = TIMEOUT) -> float:
    """实际使用的超时秒数：``VAULT_TIMEOUT``（应用设置写入）> ``fallback``。

    后端「批量验证」对话框里的超时设置经环境变量下发，每次发请求时读取——改完即时
    生效，无需重启。变量缺失或非法时回退到 ``fallback``（默认 ``TIMEOUT``）。

    ``fallback`` 供类别层用：邮箱类插件的默认超时是 15 秒而非 20 秒，它只需
    ``def timeout(): return sdk.timeout(15.0)``，不必重复实现整个函数。
    """
    raw = os.environ.get("VAULT_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return fallback


# 响应正文截断长度，用于错误消息
_BODY_LIMIT = 240


_logger = logging.getLogger("plugin")


def log(message: str) -> None:
    """插件日志 → 应用统一日志（根 logger 落盘到 data/logs/keyrealm.log）。

    不要用 print(..., file=sys.stderr)：打包成 GUI 程序后没有 stdio，
    输出会被静默丢弃（在 windowed 模式下写 stderr 甚至直接抛异常），
    结果就是插件的告警与异常在系统日志里完全不可见。
    """
    _logger.info("%s", message)


def log_debug(message: str) -> None:
    """DEBUG 级日志：只在日志级别调到 DEBUG 时才落盘。

    用于「对排查有用、但不该展示给用户」的细节（例如权限探测逐项失败的原始码），
    避免把这些说明塞进前端提示里。
    """
    _logger.debug("%s", message)


def mask_secret(value: str) -> str:
    """脱敏展示一个敏感值（保留首尾少量字符，如 ``sk-123…abcd``）。

    **写日志、拼用户可见提示时一律用它，不要自己切字符串** ——
    手写的 ``api_key[:8]`` 会把密钥片段写进日志文件与凭据的 ``status_detail``，
    与框架在 ``plugin_manager`` 出口做的净化不是一个通道，净化拦不住。

    空值返回空串；过短的值只保留首 2 位（见 `backend/crypto.py` 的权威实现）。
    """
    # 懒加载：本模块承诺「叶子模块」（只依赖标准库 + httpx），
    # 所以在函数内 import 框架模块，保住这条不变量。
    from backend.crypto import mask_secret as _impl

    return _impl(value or "")


# ── 请求封装 ─────────────────────────────────────────────

def proxy() -> Optional[str]:
    """本次请求使用的代理。

    优先取**框架按调用注入**的覆盖值（``vault_proxy_ctx``）——批量验证时每条凭据各一份、
    线程隔离，所以「不同凭据走不同代理」不会互相踩；没有覆盖时回退环境变量：
    ``VAULT_PROXY``（应用设置写入）> 标准代理变量。

    覆盖值为 ``DIRECT`` 表示该凭据选了「直连」，此时**压过**上面所有环境变量。
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


# ── TLS 版本策略（框架级） ────────────────────────────────

# 固定版本 → ssl.TLSVersion
_PINNED_VERSIONS = {
    "1.0": ssl.TLSVersion.TLSv1,
    "1.1": ssl.TLSVersion.TLSv1_1,
    "1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
}
_VALID_MODES = ("auto", *_PINNED_VERSIONS)


def tls_version() -> str:
    """本次请求的 TLS 版本策略：``auto``（默认）/ ``1.0`` / ``1.1`` / ``1.2`` / ``1.3``。

    顺序：按调用注入（``vault_tls_ctx``）> 环境变量 ``VAULT_TLS`` > ``auto``。
    ``auto`` = 先按标准协商，仅当连接阶段握手失败才回退 TLS 1.2 重试一次；固定版本 = 锁死范围。
    """
    ctx = sys.modules.get("vault_tls_ctx")
    if ctx is not None:
        value = ctx.current()
        if value is not ctx.UNSET:
            return str(value)
    raw = os.environ.get("VAULT_TLS")
    if raw:
        text = raw.strip().lower()
        if text in _VALID_MODES:
            return text
    return "auto"


def _ssl_context(pinned: Optional[str], verify) -> ssl.SSLContext:
    """按策略建 SSL 上下文：``pinned`` 为空 = auto；``verify=False`` 关证书校验。"""
    context = ssl.create_default_context()
    if verify is False:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if pinned in _PINNED_VERSIONS:
        context.minimum_version = context.maximum_version = _PINNED_VERSIONS[pinned]
    return context


def _is_tls_failure(exc: BaseException) -> bool:
    """异常链里是否为连接阶段的 TLS 失败（证书校验失败不算）。"""
    seen: set[int] = set()
    chain: list[BaseException] = []
    node: Optional[BaseException] = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = node.__cause__ or node.__context__
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
        return False
    return any(isinstance(item, (ssl.SSLError, ConnectionResetError)) for item in chain)


class _TlsPolicyTransport(httpx.HTTPTransport):
    """``auto`` 下握手在连接阶段失败时，用 TLS 1.2 重发一次（证书校验照旧）。

    放 transport 层的原因：插件调用处看不到「握手失败」这个中间状态，没有重试机会。
    ``handle_request`` 收到的是已缓冲的请求，重发不丢 body。
    """

    def __init__(self, *, tls_mode: str = "auto", verify=True, **kwargs):
        self._mode = tls_mode
        self._verify = verify
        self._kwargs = kwargs
        self._fallback: Optional[httpx.HTTPTransport] = None
        pinned = tls_mode if tls_mode in _PINNED_VERSIONS else None
        if pinned or verify is False:
            super().__init__(verify=_ssl_context(pinned, verify), **kwargs)
        else:
            super().__init__(verify=True, **kwargs)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return super().handle_request(request)
        except httpx.ConnectError as exc:
            if self._mode != "auto" or not _is_tls_failure(exc):
                raise
            if self._fallback is None:
                log("TLS 握手在连接阶段失败（疑似目标只接受特定 TLS 版本）→ 回退 TLS 1.2 重试一次")
                self._fallback = httpx.HTTPTransport(
                    verify=_ssl_context("1.2", self._verify), **self._kwargs)
            return self._fallback.handle_request(request)


def make_client(follow_redirects: bool = True,
                request_timeout: Optional[float] = None,
                verify: Optional[bool] = None) -> httpx.Client:
    """建一个 httpx 客户端（自动带上代理、超时与 TLS 版本策略）。

    ``request_timeout`` 为空时用全局超时 ``timeout()``。
    ``verify=False`` 关闭证书校验（自签内网自建用）；``None`` = 默认校验。
    """
    transport = _TlsPolicyTransport(
        tls_mode=tls_version(),
        verify=False if verify is False else True,
        proxy=proxy(),
    )
    return httpx.Client(
        timeout=timeout() if request_timeout is None else request_timeout,
        follow_redirects=follow_redirects,
        transport=transport,
    )


def safe_json(response: httpx.Response) -> Optional[dict]:
    """响应体解析为 dict；不是 JSON 或顶层非 dict 时返回 None。"""
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def body_snippet(response: httpx.Response) -> str:
    """响应正文压成一行并截断，用于把上游的原始报错带进提示里。"""
    try:
        text = response.text or ""
    except Exception:
        return ""
    return " ".join(text.split())[:_BODY_LIMIT]


def guarded(fn):
    """装饰器：兜底 verify/validate 未预期异常 → error 结果，保证不抛出。

    异常消息只暴露异常类型名，避免把 URL（部分服务密钥在 query 中）
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


# ── 写操作 / 列表类操作的通用辅助 ────────────────────────

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
    逐个试字段名容易漏。用法::

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
