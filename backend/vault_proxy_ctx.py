"""按调用生效的代理上下文 —— 框架与插件之间「本次调用走哪个代理」的通道。

为什么需要它
------------
插件的 ``make_client()`` 一直只从进程环境变量 ``VAULT_PROXY`` 取代理，那是**全局单值**。
但批量验证是并发的（``ThreadPoolExecutor``，每条凭据一个任务），不同凭据可能要走不同出口
——用环境变量会互相踩，A 凭据的代理会串到 B 凭据的请求上。

设计
----
- 本模块由**框架持有**；``main.py`` 启动时以固定别名注册进 ``sys.modules``：
  ``sys.modules.setdefault("vault_proxy_ctx", vault_proxy_ctx)``
- 插件侧 ``backend/plugin_sdk.py`` 的 ``proxy()`` **懒查** ``sys.modules.get("vault_proxy_ctx")``：
  插件不直接 import backend 内部模块，只经 SDK 取用；脱离主程序单独跑时该模块不在，
  自然回退到环境变量，不需要 try/except。
- 值存在 ``threading.local()`` 里——每个并发任务各一份，天然隔离；线程池复用的 worker
  靠 ``begin/end`` 成对还原，不会把上一条凭据的代理带给下一条。

两个哨兵必须分开
----------------
``UNSET`` 与 ``DIRECT`` 不能用同一个 ``None`` 表达：那样「跟随全局」在外部设了
``ALL_PROXY`` / ``HTTPS_PROXY`` 时就没法正确回退了（会把「未指定」误当成「直连」）。
"""
from __future__ import annotations

import threading
from typing import Union

# 未指定 → 回退环境变量（保持「外部 env 优先于界面设置」的老契约）
UNSET = object()
# 本次强制直连，压过 VAULT_PROXY / ALL_PROXY / HTTPS_PROXY
DIRECT = object()

_tls = threading.local()

Value = Union[object, str]   # UNSET | DIRECT | 代理 URL


def current() -> Value:
    """本线程当前的覆盖值：``UNSET`` / ``DIRECT`` / 代理 URL 字符串。"""
    return getattr(_tls, "value", UNSET)


def begin(value: Value) -> Value:
    """设置本线程的覆盖值，返回旧值；``end()`` 用它还原（支持嵌套）。"""
    previous = current()
    _tls.value = value
    return previous


def end(token: Value) -> None:
    """把覆盖值还原成 ``begin()`` 返回的旧值。"""
    _tls.value = token
