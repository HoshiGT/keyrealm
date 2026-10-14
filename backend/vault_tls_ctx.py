"""按调用生效的 TLS 版本策略上下文 —— 框架与插件之间「本次调用用哪个 TLS 版本」的通道。

与 ``vault_proxy_ctx`` 同构：``main.py`` 以固定别名注册进 ``sys.modules``，插件侧
``plugin_sdk.tls_version()`` 懒查；值放 ``threading.local()``，并发调用互不干扰。
"""
from __future__ import annotations

import threading
from typing import Union

# 未指定 → 回退 VAULT_TLS（最终默认 auto）
UNSET = object()

_tls = threading.local()

# UNSET | "auto" | "1.0" | "1.1" | "1.2" | "1.3"
Value = Union[object, str]


def current() -> Value:
    """本线程当前的覆盖值。"""
    return getattr(_tls, "value", UNSET)


def begin(value: Value) -> Value:
    """设置本线程覆盖值并返回旧值（``end()`` 用它还原，支持嵌套）。"""
    previous = current()
    _tls.value = value
    return previous


def end(token: Value) -> None:
    """还原成 ``begin()`` 返回的旧值。"""
    _tls.value = token
