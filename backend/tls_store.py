"""TLS 版本策略：逐级解析 + 按调用注入。

与 ``proxy_store`` 同一套继承，但没有注册表——TLS 只是一个枚举串：

1. 凭据 ``data["_tls_version"]``（保留键，空 = 跟随上层）
2. 插件级默认 / 3. 类别级默认（``preset_store``）
4. 全局 ``settings.tls_version``（默认 ``auto``），由 ``apply_global()`` 写进 ``VAULT_TLS``

``auto``：先按标准协商，仅当连接阶段握手失败才回退 TLS 1.2 重试一次（见 ``plugin_sdk``）。
固定 ``1.0/1.1/1.2/1.3``：锁死协商范围。
"""
from __future__ import annotations

import contextlib
import os
from typing import Optional, Union

from . import database as db
from . import vault_tls_ctx as ctx

# 凭据 data 里的保留键（与 proxy_store 的 _proxy_mode / _proxy_id 并列）
KEY = "_tls_version"

SETTING_GLOBAL = "tls_version"
ENV_KEY = "VAULT_TLS"

DEFAULT = "auto"
AUTO = "auto"
VERSIONS = ("auto", "1.0", "1.1", "1.2", "1.3")

# 供调用方判定「本层没指定、继续回退全局」（与 vault_tls_ctx.UNSET 是同一个哨兵）
UNSET = ctx.UNSET

# 本进程由应用写入 VAULT_TLS 的那个值（用于回收，避免压掉外部显式设置）
_applied: Optional[str] = None


def normalize(value) -> str:
    """收敛成合法版本串；不认识的回落 ``auto``。"""
    text = str(value or "").strip().lower()
    return text if text in VERSIONS else DEFAULT


def _known(value) -> str:
    """合法才返回，否则空串（用于「本层未指定」判定）。"""
    text = str(value or "").strip().lower()
    return text if text in VERSIONS else ""


# ── 全局默认 ─────────────────────────────────────────────

def global_value() -> str:
    return normalize(db.get_setting(SETTING_GLOBAL, DEFAULT))


def set_global(value) -> dict:
    """设置全局默认版本并立即生效。"""
    version = normalize(value)
    db.set_setting(SETTING_GLOBAL, version)
    apply_global()
    return {"ok": True, "tls_version": version}


def apply_global() -> str:
    """把全局默认写进 ``VAULT_TLS``；外部已显式设置该变量时不覆盖。"""
    global _applied
    # 1) 回收上一次由应用写入的值
    if _applied is not None and os.environ.get(ENV_KEY) == _applied:
        os.environ.pop(ENV_KEY, None)
    _applied = None
    # 2) 外部已显式设置 → 优先
    if os.environ.get(ENV_KEY):
        return normalize(os.environ[ENV_KEY])
    # 3) 写入本次配置
    value = global_value()
    os.environ[ENV_KEY] = value
    _applied = value
    return value


# ── 逐级解析 ─────────────────────────────────────────────

def inherited(credential_type: str) -> str:
    """插件级 / 类别级默认继承到的版本（preset_store 已按「插件级覆盖类别级」合并）；无则空串。"""
    if not credential_type:
        return ""
    # 延迟导入：preset_store 依赖本模块的保留键常量，模块级导入会成环
    from . import preset_store

    values = preset_store.defaults_for(credential_type) or {}
    return _known(values.get(KEY))


def resolve(data: dict, credential_type: str = "") -> Union[object, str]:
    """逐层解析：凭据 → 插件/类别预设 → ``ctx.UNSET``（回退 ``VAULT_TLS``）。"""
    own = _known((data or {}).get(KEY))
    if own:
        return own
    inherited_value = inherited(credential_type)
    if inherited_value:
        return inherited_value
    return ctx.UNSET


def describe_inherited(credential_type: str) -> str:
    """该凭据「跟随上层」时最终会用的版本（给下拉括注）。"""
    return inherited(credential_type) or global_value()


@contextlib.contextmanager
def credential_tls_scope(data: dict, credential_type: str = ""):
    """本次调用期间生效「该凭据的 TLS 版本」，退出时还原。"""
    resolved = resolve(data, credential_type)
    token = ctx.begin(resolved)
    try:
        yield resolved
    finally:
        ctx.end(token)


# ── 供界面 ───────────────────────────────────────────────

def options() -> dict:
    """全局默认 + 全部可选值，供「系统设置」渲染。"""
    return {"tls_version": global_value(), "tls_versions": list(VERSIONS)}
