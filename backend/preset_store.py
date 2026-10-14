"""类别级 / 插件级默认设置：新建凭据时按「插件级 > 类别级 > manifest 默认」继承。

框架只提供**存储 + 继承 + 注入**这三件事；「哪些字段可以预设」由插件自己在 manifest 里声明：

- 默认：**全部非敏感字段**都可预设（``base_url``、``notes``、``probe_model`` …）。
- 想排除某个字段：在 manifest 该字段上加 ``"preset": false``。
- **敏感字段（api_key / token / 密码）永远不可预设**——``preset_defaults.values`` 是**明文列**，
  一旦密钥被写成默认值就是明文落库，比凭据数据泄露更糟。写入、读取、注入三处各剔一次。

代理（``_proxy_mode`` / ``_proxy_id``）与 TLS 版本（``_tls_version``）不是 manifest 字段，
作为保留键单独放行，见 ``proxy_store`` / ``tls_store``。
"""
from __future__ import annotations

from typing import Optional

from . import database as db
from .proxy_store import KEY_ID, KEY_MODE
from .tls_store import KEY as KEY_TLS

SCOPE_CATEGORY = "category"
SCOPE_PLUGIN = "plugin"

# 与 manifest 字段无关、但允许预设的保留键
_RESERVED_KEYS = {KEY_MODE, KEY_ID, KEY_TLS}


def _iter_fields(record: dict):
    for field in record.get("fields_schema") or []:
        if isinstance(field, dict):
            yield field


def _is_presetable(field: dict) -> bool:
    key = field.get("key") or ""
    if not key or key.startswith("_"):
        return False
    if field.get("sensitive"):
        return False
    return field.get("preset") is not False


def _plugin_keys(record: Optional[dict]) -> set[str]:
    if not record:
        return set()
    return {f["key"] for f in _iter_fields(record) if _is_presetable(f)}


def _category_keys(category: str) -> set[str]:
    """类别可预设键 = 子插件 schema 的并集，但**任一个子插件里是敏感/禁用的键一律排除**。

    后者是必须的：类别默认是给整组插件用的，只要有一个插件把该 key 当密钥，
    就不能让它出现在类别级的明文默认里。
    """
    allowed: set[str] = set()
    banned: set[str] = set()
    for record in db.list_plugins():
        if (record.get("category") or "") != category:
            continue
        for field in _iter_fields(record):
            key = field.get("key") or ""
            if not key or key.startswith("_"):
                continue
            if _is_presetable(field):
                allowed.add(key)
            else:
                banned.add(key)
    return allowed - banned


def preset_keys(credential_type: str) -> set[str]:
    """某个插件允许被预设的字段键（不含保留键）。"""
    return _plugin_keys(db.get_plugin_by_type(credential_type))


def allowed_keys(scope: str, scope_key: str, credential_type: str = "") -> set[str]:
    """某作用域允许写入的键 = 可预设字段键 + 代理保留键。"""
    if scope == SCOPE_PLUGIN:
        return preset_keys(credential_type or scope_key) | _RESERVED_KEYS
    return _category_keys(scope_key) | _RESERVED_KEYS


# ── 读写 ─────────────────────────────────────────────────

def get_scope_defaults(scope: str, scope_key: str) -> dict:
    """原始默认值（不做过继承与过滤），供设置界面回填。"""
    return db.get_preset_defaults(scope, scope_key)


def set_scope_defaults(scope: str, scope_key: str, values: dict,
                       credential_type: str = "") -> dict:
    """覆盖写入某作用域的默认值；**写入前按白名单与敏感字段过滤一遍**。"""
    if scope not in (SCOPE_CATEGORY, SCOPE_PLUGIN):
        return {"ok": False, "message": f"无效作用域：{scope}"}
    allowed = allowed_keys(scope, scope_key, credential_type)
    cleaned: dict = {}
    for key, value in (values or {}).items():
        if key not in allowed:
            continue
        if key in _RESERVED_KEYS and value in (None, ""):
            continue
        cleaned[key] = value
    db.set_preset_defaults(scope, scope_key, cleaned)
    return {"ok": True, "scope": scope, "scope_key": scope_key, "values": cleaned}


def clear_scope_defaults(scope: str, scope_key: str) -> dict:
    db.delete_preset_defaults(scope, scope_key)
    return {"ok": True, "scope": scope, "scope_key": scope_key}


def defaults_for(credential_type: str) -> dict:
    """该插件新建凭据时应带的默认值：插件级覆盖类别级，再按可预设键过滤。"""
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return {}
    merged = dict(get_scope_defaults(SCOPE_CATEGORY, record.get("category") or ""))
    merged.update(get_scope_defaults(SCOPE_PLUGIN, credential_type))
    # 读取时再过滤一次：类别级可能残留「别的子插件没有」的键，直接丢掉即可（无害）
    allowed = preset_keys(credential_type) | _RESERVED_KEYS
    return {k: v for k, v in merged.items() if k in allowed}


def apply_defaults(credential_type: str, data: Optional[dict]) -> dict:
    """把默认值补进待创建的数据；**显式传入的键优先**（含显式传空串 = 就是要空）。"""
    merged = dict(defaults_for(credential_type))
    merged.update(data or {})
    return merged


# ── 供设置界面渲染 ────────────────────────────────────────

def category_schema_union(category: str) -> list[dict]:
    """某类别下所有子插件字段的并集（只留可预设的），供类别默认设置对话框渲染。"""
    allowed = _category_keys(category)
    seen: dict[str, dict] = {}
    for record in db.list_plugins():
        if (record.get("category") or "") != category:
            continue
        for field in _iter_fields(record):
            key = field.get("key") or ""
            if key in allowed and key not in seen:
                seen[key] = field
    return list(seen.values())


def plugin_schema(credential_type: str) -> list[dict]:
    """某插件可预设的字段列表，供插件默认设置对话框渲染。"""
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return []
    return [f for f in _iter_fields(record) if _is_presetable(f)]


def scope_options(scope: str, scope_key: str, credential_type: str = "") -> dict:
    """一次性返回某个作用域设置界面需要的全部数据。"""
    return {
        "scope": scope,
        "scope_key": scope_key,
        "values": get_scope_defaults(scope, scope_key),
        "fields": plugin_schema(credential_type or scope_key)
        if scope == SCOPE_PLUGIN else category_schema_union(scope_key),
        "reserved": sorted(_RESERVED_KEYS),
    }
