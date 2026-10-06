"""邮箱 类别共享模块：**代理隧道**工具（邮箱专属）。

通用工具（结果类型、异常兜底、日志、超时）已上收框架层的 ``backend/plugin_sdk.py``，
本模块**不再重复**它们 —— 需要时直接 ``from backend.plugin_sdk import ...``。

这里只留邮箱类别特有的东西：**把普通 TCP 连接穿过 SOCKS5 / HTTP 代理**。
`smtplib` / `imaplib` 只会自己 ``socket.create_connection``，用不了 httpx 那一套；
但它们跑的是普通 TCP，可以用 PySocks 建一个「已经穿过代理」的 socket 再交给标准库
——见下方 ``Tunneled*`` 子类，只覆写建连那一处。

具体服务（如 SMTP）的连接、登录与结果判定全部下沉到各自
``plugins/mail/<服务>/plugin.py``（服务之间允许重复，刻意为之）。

本模块的默认超时是 **15 秒**（比其它类别的 20 秒短，邮箱握手通常更快），由本模块导出的
``timeout()`` 承载 —— 子插件请 ``from mail_common import timeout``，不要从 SDK 取，
否则会拿到 20 秒的通用默认值。

依赖方向：`plugin.py` → `mail_common.py` → `backend.plugin_sdk`。只依赖标准库与 SDK。
"""
from __future__ import annotations

import imaplib
import smtplib
import socket
from urllib.parse import unquote

from backend.plugin_sdk import proxy, timeout as _sdk_timeout

# 请求超时（秒）—— 本类别默认值；实际生效值走 timeout()
TIMEOUT = 15.0


def timeout() -> float:
    """本类别实际使用的超时秒数：``VAULT_TIMEOUT``（应用设置写入）> ``TIMEOUT``（15 秒）。

    实现委托给 ``backend.plugin_sdk.timeout(TIMEOUT)``，只是把类别默认值换掉，
    不再重复整套「读环境变量」逻辑。
    """
    return _sdk_timeout(TIMEOUT)


# ── 代理：邮箱走 TCP 隧道（不是 HTTP 代理）────────────────
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
    代理串取 ``backend.plugin_sdk.proxy()``（框架按调用注入 > 环境变量）。
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
