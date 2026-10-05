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

# httpx 走 SOCKS 依赖独立的 socksio 包（由 httpx[socks] 提供）。
# 它在 httpx 内部是「函数体内的静态 import」，打包时若构建机没装，PyInstaller 不会报错，
# 直到运行时用 socks5 代理才炸出一句英文 ImportError——所以这里显式探测并给中文提示。
_SOCKS_SCHEMES = {"socks4", "socks5", "socks5h"}

SOCKS_HINT = (
    "当前 Python 环境缺少 SOCKS 支持：httpx 走 socks 需要 socksio 包，但没装。"
    "处理办法——开发机执行 pip install \"httpx[socks]\" 后重启应用；"
    "若是打包好的 exe，需在构建机上装好后用 python scripts/build.py --frontend 重新打包。"
)


def normalize(value: str | None) -> str:
    """去空白，返回规范化后的代理串（可能为空）。"""
    return (value or "").strip()


def scheme(value: str | None) -> str:
    """取代理串的协议名（小写）；无 ``://`` 前缀时返回空串。"""
    text = normalize(value)
    if "://" not in text:
        return ""
    return text.split("://", 1)[0].lower()


def needs_socks(value: str | None) -> bool:
    """该代理串是否需要 SOCKS 支持（socks4 / socks5 / socks5h）。"""
    return scheme(value) in _SOCKS_SCHEMES


def socks_available() -> bool:
    """当前环境能否走 SOCKS。

    真去 ``import socksio``（而非 find_spec）：冻结进 exe 的模块只有被真正导入过才可靠，
    顺带把它塞进 sys.modules，后面 httpx 再导入就是命中缓存。
    """
    try:
        import socksio  # noqa: F401
    except Exception:
        return False
    return True


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
