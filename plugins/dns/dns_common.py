"""域名解析 类别共享模块：底层通用工具 + 子插件原型接口。

只提供与具体服务无关的最底层能力：

- 结果类型 `VerifyResult`
- 代理读取 `proxy`、HTTP 客户端 `make_client`
- 响应解析 `safe_json` / `body_snippet`
- 异常兜底 `guarded`、日志 `log`
- 超时 `TIMEOUT`（默认值）/ `timeout()`（实际生效值，读 `VAULT_TIMEOUT`）
- 常量 `_BODY_LIMIT`

具体服务的端点、字段校验、判定等实现写在各自 plugins/dns/<服务>/plugin.py 内
（服务之间允许重复，刻意为之）；新增服务插件照抄同级子插件即可，
无需修改本模块。协议级、跨服务完全相同的工具可另建工具模块供子插件复用。

本模块完全独立，不导入 backend 包，可脱离主程序单独运行。
"""
from __future__ import annotations

import logging
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


_logger = logging.getLogger("plugin.dns")


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


# ── 子插件原型接口（子插件应实现；这里只声明契约，运行时不调用本模块的实现）──

def validate(data: dict) -> list:
    """子插件实现：返回配置错误列表（空列表 = 通过）。"""
    raise NotImplementedError


def verify(data: dict) -> "VerifyResult":
    """子插件实现：验证凭据，返回 VerifyResult（valid / invalid / error）。"""
    raise NotImplementedError


def get_display_summary(data: dict) -> str:
    """子插件实现：凭据列表中的简短展示文本。"""
    return ""


def actions(data: dict) -> list:
    """子插件可选实现：声明可用操作；返回 [] 表示无操作。"""
    return []


def run_action(action_id: str, params: dict, data: dict) -> dict:
    """子插件可选实现：执行操作。"""
    return {"ok": False, "error": "未实现"}
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
