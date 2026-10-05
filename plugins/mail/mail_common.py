"""邮箱 类别共享模块：底层通用工具 + 子插件原型接口。

只提供与具体服务无关的最底层能力：

- 结果类型 `VerifyResult`
- 异常兜底 `guarded`、日志 `log`
- 超时 `TIMEOUT`（默认值）/ `timeout()`（实际生效值，读 `VAULT_TIMEOUT`）

具体服务（如 SMTP）的连接、登录与结果判定全部下沉到各自
plugins/mail/<服务>/plugin.py（服务之间允许重复，刻意为之）；
新增邮箱类插件照抄 smtp 子插件即可，无需修改本模块。

本类别走标准库 `smtplib`，不涉及 httpx / 代理；本模块也不导入 backend 包，
可脱离主程序单独运行。
"""
from __future__ import annotations

import imaplib
import logging
import os
import smtplib
import socket
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote


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


_logger = logging.getLogger("plugin.mail")


def log(message: str) -> None:
    """插件日志 → 应用统一日志（根 logger 落盘到 data/logs/credential_vault.log）。

    不要改回 print(..., file=sys.stderr)：打包成 GUI 程序后没有 stdio，
    输出会被静默丢弃（在 windowed 模式下写 stderr 甚至直接抛异常），
    结果就是插件的告警与异常在系统日志里完全不可见。
    """
    _logger.info("%s", message)


# 请求超时（秒）—— 本类别默认值；实际生效值走 timeout()
TIMEOUT = 15.0


def timeout() -> float:
    """实际使用的超时秒数：``VAULT_TIMEOUT``（应用设置写入）> 模块默认 ``TIMEOUT``。

    后端「批量验证」对话框里的超时设置经环境变量下发（插件是独立模块，不允许
    import backend），每次连接时读取——改完即时生效，无需重启。变量缺失或非法时
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


def guarded(fn):
    """装饰器：兜底 verify/validate 未预期异常 → error 结果，保证不抛出。

    异常消息只暴露异常类型名，避免把 host / 授权码等敏感信息带进错误提示。
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 插件边界，必须兜底
            log(f"verify 未预期异常: {type(exc).__name__}")
            return VerifyResult(status="error", message=f"插件执行异常（{type(exc).__name__}）")

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


# ── 代理：邮箱走 TCP 隧道（不是 HTTP 代理）────────────────
#
# smtplib / imaplib 只会自己 socket.create_connection，用不了 httpx 那一套；但它们跑的是
# 普通 TCP，可以用 SOCKS5 / HTTP CONNECT 把连接**隧道**穿出去。这里用 PySocks 建一个
# 「已经穿过代理」的 socket，再交给标准库——见下方 Tunneled* 子类，只覆写建连那一处。
try:
    import socks as _socks          # PySocks
except Exception:                    # noqa: BLE001 - 没装时给中文处理办法，而不是甩 ImportError
    _socks = None

SOCKS_HINT = (
    "邮箱走代理需要 PySocks 包，但当前环境没有。处理办法——开发机执行 pip install PySocks "
    "后重启应用；若是打包好的 exe，需在构建机上装好后用 python scripts/build.py --frontend 重新打包。"
)

# scheme → (PySocks 类型名, 是否让代理解析域名)
_PROXY_SCHEMES = {
    "socks5": ("SOCKS5", False),
    "socks5h": ("SOCKS5", True),     # h = 远端解析（域名交给代理，本地不做 DNS）
    "socks4": ("SOCKS4", False),
    "socks4a": ("SOCKS4", True),
    "http": ("HTTP", True),          # HTTP CONNECT 隧道
    "https": ("HTTP", True),
}
_PROXY_DEFAULT_PORTS = {"socks5": 1080, "socks5h": 1080, "socks4": 1080, "socks4a": 1080,
                        "http": 8080, "https": 8080}


def proxy() -> Optional[str]:
    """本次连接使用的代理（与其它类别同一套解析）。

    先取**框架按调用注入**的覆盖值（``vault_proxy_ctx``，批量验证时每条凭据各一份、线程隔离），
    没有覆盖再回退环境变量：``VAULT_PROXY``（应用设置写入）> 标准代理变量。
    覆盖值为 ``DIRECT`` 表示该凭据选了「直连」，此时压过上面所有环境变量。
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


def parse_proxy_url(url: str) -> tuple:
    """解析代理串 → ``(scheme, host, port, username, password)``。

    入参形如 ``socks5h://user:pass@127.0.0.1:10808``；用户名/密码里的百分号编码会还原。
    """
    text = str(url or "").strip()
    scheme, _, rest = text.partition("://")
    scheme = scheme.lower()
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    username = password = None
    if "@" in rest:
        cred, _, rest = rest.rpartition("@")
        raw_user, _, raw_pwd = cred.partition(":")
        username = unquote(raw_user) or None
        password = unquote(raw_pwd) or None
    host, sep, port_text = rest.rpartition(":")
    if not sep:                      # 没写端口 → 整段都是 host
        host, port_text = rest, ""
    try:
        port = int(port_text) if port_text else _PROXY_DEFAULT_PORTS.get(scheme, 1080)
    except ValueError:
        raise RuntimeError(f"代理端口不是数字：{text}")
    if not host:
        raise RuntimeError(f"代理地址不合法：{text}")
    return scheme, host, port, username, password


def open_socket(host: str, port: int, timeout):
    """建立到 ``(host, port)`` 的 TCP 连接；配了代理就穿过 SOCKS / HTTP CONNECT 隧道。

    没配代理、或该凭据选了「直连」时，就是普通的 ``socket.create_connection``。
    """
    if timeout is not None and not timeout:
        raise ValueError("Non-blocking socket (timeout=0) is not supported")

    proxy_url = proxy()
    if not proxy_url:
        return socket.create_connection((host, port), timeout)

    if _socks is None:
        raise RuntimeError(SOCKS_HINT)

    scheme, addr, pport, user, pwd = parse_proxy_url(proxy_url)
    mapped = _PROXY_SCHEMES.get(scheme)
    if mapped is None:
        raise RuntimeError(
            f"不支持的代理协议「{scheme or '(缺失)'}」——邮箱可用 socks5 / socks5h / socks4 / http"
        )
    type_name, rdns = mapped
    return _socks.create_connection(
        (host, port), timeout=timeout,
        proxy_type=getattr(_socks, type_name),
        proxy_addr=addr, proxy_port=pport, proxy_rdns=rdns,
        proxy_username=user, proxy_password=pwd,
    )


# 只覆写「建连」那一处，其余 SMTP/IMAP 行为与标准库完全一致。
class TunneledSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return open_socket(host, port, timeout)


class TunneledSMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = open_socket(host, port, timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


class TunneledIMAP4(imaplib.IMAP4):
    def _create_socket(self, timeout):
        return open_socket(self.host, self.port, timeout)


class TunneledIMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        sock = open_socket(self.host, self.port, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


# ── 子插件原型接口（子插件应实现；这里只声明契约，运行时不调用本模块的实现）──

def validate(data: dict) -> list:
    """子插件实现：返回配置错误列表（空列表 = 通过）。"""
    raise NotImplementedError


def verify(data: dict) -> "VerifyResult":
    """子插件实现：连接并验证凭据，返回 VerifyResult。"""
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
