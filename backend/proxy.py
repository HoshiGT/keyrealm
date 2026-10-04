"""验证代理配置。

把「系统设置」中的验证代理写入环境变量 ``VAULT_PROXY``，供各插件在 verify 时读取；
插件保持独立（不 import backend），只从环境变量取代理，解析优先级：

    VAULT_PROXY  >  ALL_PROXY/all_proxy  >  HTTPS_PROXY/https_proxy

外部也可直接导出 ``VAULT_PROXY`` 覆盖界面设置：应用只清理「自己写入过」的值，
若该变量是外部设置的，则应用不会覆盖它（环境变量优先）。
"""
from __future__ import annotations

import os

ENV_KEY = "VAULT_PROXY"

# 记录应用自身写入环境变量的值，用于切换/清空时精确回收，避免误删外部同名变量
_applied: str | None = None

_ALLOWED_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


def normalize(value: str | None) -> str:
    """去空白，返回规范化后的代理串（可能为空）。"""
    return (value or "").strip()


def is_valid(value: str | None) -> bool:
    """校验代理串：空串合法（表示直连）；否则需带受支持的协议前缀。"""
    text = normalize(value)
    if not text:
        return True
    if "://" not in text:
        return False
    return text.split("://", 1)[0].lower() in _ALLOWED_SCHEMES


def apply_proxy(value: str | None) -> str | None:
    """把代理写入 ``os.environ[VAULT_PROXY]``；空值则清除应用此前写入的值。

    返回实际生效的代理串（None 表示直连）。
    """
    global _applied
    text = normalize(value) or None

    # 1) 回收上一次由应用写入的值
    if _applied is not None and os.environ.get(ENV_KEY) == _applied:
        os.environ.pop(ENV_KEY, None)
    _applied = None

    # 2) 若外部已显式设置该变量，则环境变量优先，不覆盖
    if os.environ.get(ENV_KEY):
        return os.environ.get(ENV_KEY)

    # 3) 写入本次配置
    if text:
        os.environ[ENV_KEY] = text
        _applied = text
    return text
