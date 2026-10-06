"""批量验证的运行参数：并发线程数与请求超时。

两个参数都在「批量验证」对话框里就地调整，点「验证」时随本次验证一起持久化到
``settings`` 表（``batch_concurrency`` / ``verify_timeout``），下次打开对话框时作为默认值带回。
调度器触发的定时批量验证也读同一份设置——因此这里是这两项参数的**单一事实来源**。

超时为什么走环境变量
--------------------
插件是独立模块，**只允许 import ``backend.plugin_sdk``**（不得 import backend 的其它模块，
见 docs/PLUGIN_GUIDE.md §3），后端与插件之间没有别的参数通道，只能借环境变量——与
``VAULT_PROXY`` 完全同一套做法。``backend/plugin_sdk.py`` 的 ``timeout()`` 在每次发请求时读
``VAULT_TIMEOUT``（类别层要换默认值就传它的 ``fallback`` 形参），所以改完立即生效、无需重启。
外部显式设置的 ``VAULT_TIMEOUT`` 优先于界面设置。

> 「欠费探测」是**单一凭据层面**的配置（凭据自身的 `probe_chat` / `probe_model` 字段），
> 不经过本模块——详见 docs/PLUGIN_GUIDE.md 第 2.4 节。
"""
from __future__ import annotations

import os

from . import database as db

# 并发线程数（每个凭据一个任务）
DEFAULT_CONCURRENCY = 8
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 32

# 单次请求超时（秒）
DEFAULT_TIMEOUT = 20.0
MIN_TIMEOUT, MAX_TIMEOUT = 5.0, 300.0

# 超时的环境变量通道（插件侧 timeout() 读它）
ENV_KEY = "VAULT_TIMEOUT"

SETTING_CONCURRENCY = "batch_concurrency"
SETTING_TIMEOUT = "verify_timeout"

# 记录进程启动时该变量是否已被外部设置；是则外部值优先，界面设置不再覆盖它
_external_env = os.environ.get(ENV_KEY)


def _given(value) -> bool:
    """前端传值是否有效（None / 空串 / 纯空白都视为「未传，用已保存的设置」）。"""
    return value is not None and str(value).strip() != ""


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _clamp_float(value, default: float, lo: float, hi: float) -> float:
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def concurrency(value=None) -> int:
    """并发线程数：优先用传入值（前端本次选择），否则读设置，都做范围收敛。"""
    if not _given(value):
        value = db.get_setting(SETTING_CONCURRENCY, "")
    return _clamp_int(value, DEFAULT_CONCURRENCY, MIN_CONCURRENCY, MAX_CONCURRENCY)


def timeout(value=None) -> float:
    """请求超时秒数：优先用传入值，否则读设置，都做范围收敛。"""
    if not _given(value):
        value = db.get_setting(SETTING_TIMEOUT, "")
    return _clamp_float(value, DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT)


def limits() -> dict:
    """取值边界（前端 el-input-number 的 min/max 由此下发，避免两端各写一份常量）。"""
    return {
        "concurrency_range": [MIN_CONCURRENCY, MAX_CONCURRENCY],
        "timeout_range": [MIN_TIMEOUT, MAX_TIMEOUT],
        "defaults": {"concurrency": DEFAULT_CONCURRENCY, "timeout": DEFAULT_TIMEOUT},
    }


def get_options() -> dict:
    """当前设置 + 边界，供「批量验证」对话框打开时回填。"""
    return {"concurrency": concurrency(), "timeout": timeout(), **limits()}


def apply_timeout() -> float:
    """把超时写入 ``VAULT_TIMEOUT`` 供插件读取，返回实际生效值。

    外部已显式设置该变量时不动它（与 ``VAULT_PROXY`` 的优先级约定一致）。
    """
    if _external_env is not None:
        return _clamp_float(_external_env, DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT)
    value = timeout()
    os.environ[ENV_KEY] = f"{value:g}"
    return value


def save(concurrency_value=None, timeout_value=None) -> dict:
    """持久化本次选择并立即生效，返回与 ``get_options()`` 同构的结果。

    两个值都可以不传（只改其一）；传了才会落库，避免把对话框里的半成品状态写死。
    """
    if _given(concurrency_value):
        db.set_setting(SETTING_CONCURRENCY, str(concurrency(concurrency_value)))
    if _given(timeout_value):
        db.set_setting(SETTING_TIMEOUT, f"{timeout(timeout_value):g}")
    apply_timeout()
    return get_options()
