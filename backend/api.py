"""
pywebview JS-API 桥接层
所有前端调用的方法都在这里定义
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from . import database as db
from . import mcp_server
from . import plugin_manager
from . import scheduler
from . import tray
from .crypto import (
    check_verify_token,
    create_verify_token,
    decrypt_dict,
    derive_key,
    encrypt_dict,
    generate_salt,
    generate_token,
    hash_token,
)

# 内存中的 AES 密钥（解锁后存在，锁定后清除）
_master_key: Optional[bytes] = None
_key_lock = threading.Lock()

# 哨兵：区分“未提供该参数”与“显式传 None（如清除分组）”
_UNSET = object()

# pywebview window 引用（由 main.py 注入）
_window = None


def set_window(window):
    global _window
    _window = window


def _get_key() -> Optional[bytes]:
    with _key_lock:
        return _master_key


def _set_key(key: Optional[bytes]):
    global _master_key
    with _key_lock:
        _master_key = key


def push_event(event_name: str, detail: Any = None):
    """向前端推送自定义事件"""
    if _window is None:
        return
    try:
        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
        js = f"window.dispatchEvent(new CustomEvent('{event_name}', {{detail: {payload}}}));"
        _window.evaluate_js(js)
    except Exception as e:
        print(f"[api] 事件推送失败 {event_name}: {e}")


def get_tray_status() -> dict:
    return {
        "mcp_running": mcp_server.is_running(),
        "mcp_port": mcp_server.get_port(),
        "active_tokens": db.count_active_tokens(),
        "is_locked": _get_key() is None,
    }


# ── 初始化回调注入 ────────────────────────────────────────

def init_callbacks():
    scheduler.set_callbacks(push_event, _get_key)
    mcp_server.set_callbacks(_get_key, push_event)


# ── Auth / Lock ───────────────────────────────────────────

def is_initialized() -> dict:
    verify_token = db.get_setting("verify_token")
    return {"initialized": bool(verify_token)}


def is_unlocked() -> dict:
    return {"unlocked": _get_key() is not None}


def setup_master_password(password: str) -> dict:
    if not password or len(password) < 6:
        return {"ok": False, "message": "密码至少 6 位"}
    if db.get_setting("verify_token"):
        return {"ok": False, "message": "主密码已设置，请使用解锁功能"}
    salt = generate_salt()
    key = derive_key(password, salt)
    verify_token = create_verify_token(key)
    db.set_setting("salt", salt.hex())
    db.set_setting("verify_token", verify_token)
    _set_key(key)
    return {"ok": True, "message": "主密码设置成功"}


def unlock(password: str) -> dict:
    salt_hex = db.get_setting("salt")
    verify_token = db.get_setting("verify_token")
    if not salt_hex or not verify_token:
        return {"ok": False, "message": "尚未初始化，请先设置主密码"}
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return {"ok": False, "message": "数据损坏"}
    key = derive_key(password, salt)
    if not check_verify_token(key, verify_token):
        return {"ok": False, "message": "密码错误"}
    _set_key(key)
    tray.update_tray()
    return {"ok": True, "message": "解锁成功"}


def lock_vault() -> dict:
    _set_key(None)
    tray.update_tray()
    return {"ok": True}


def change_master_password(old_password: str, new_password: str) -> dict:
    key = _get_key()
    if key is None:
        return {"ok": False, "message": "请先解锁"}
    salt_hex = db.get_setting("salt")
    verify_token = db.get_setting("verify_token")
    try:
        old_salt = bytes.fromhex(salt_hex)
    except Exception:
        return {"ok": False, "message": "数据损坏"}
    old_key = derive_key(old_password, old_salt)
    if not check_verify_token(old_key, verify_token):
        return {"ok": False, "message": "原密码错误"}
    if not new_password or len(new_password) < 6:
        return {"ok": False, "message": "新密码至少 6 位"}

    # 重新加密所有凭据
    new_salt = generate_salt()
    new_key = derive_key(new_password, new_salt)
    creds = db.list_credentials()
    for cred in creds:
        try:
            data = decrypt_dict(old_key, cred["data_encrypted"])
            new_encrypted = encrypt_dict(new_key, data)
            db.update_credential(cred["id"], data_encrypted=new_encrypted)
        except Exception as e:
            print(f"[api] 重新加密失败 cred_id={cred['id']}: {e}")

    new_verify = create_verify_token(new_key)
    db.set_setting("salt", new_salt.hex())
    db.set_setting("verify_token", new_verify)
    _set_key(new_key)
    return {"ok": True, "message": "主密码修改成功"}


# ── Credentials ───────────────────────────────────────────

def list_credentials(
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
) -> list:
    return db.list_credentials(group_id=group_id, credential_type=credential_type,
                                status=status, search=search)


def get_credential(credential_id: int) -> Optional[dict]:
    cred = db.get_credential(credential_id)
    if not cred:
        return None
    # GUI 编辑表单需要完整明文（含敏感字段）来预填，避免提交空值覆盖密钥。
    # 保险库已解锁（_get_key 可用）才返回明文；锁定时不附加 data 字段。
    key = _get_key()
    if key and cred.get("data_encrypted"):
        try:
            cred["data"] = decrypt_dict(key, cred["data_encrypted"])
        except Exception:
            cred["data"] = cred.get("data_plain", {})
    return cred


def create_credential(
    name: str,
    credential_type: str,
    data: dict,
    group_id: Optional[int] = None,
    auto_verify: bool = True,
) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked", "message": "保险库已锁定"}

    errors = plugin_manager.validate_credential(credential_type, data)
    if errors:
        return {"error": "validation_failed", "details": errors}

    plugin_record = db.get_plugin_by_type(credential_type)
    sensitive_keys = set()
    if plugin_record:
        for f in plugin_record.get("fields_schema", []):
            if f.get("sensitive"):
                sensitive_keys.add(f["key"])

    data_plain = {k: v for k, v in data.items() if k not in sensitive_keys}
    encrypted = encrypt_dict(key, data)

    cred = db.create_credential(
        name=name,
        credential_type=credential_type,
        data_encrypted=encrypted,
        data_plain=data_plain,
        group_id=group_id,
    )

    if auto_verify:
        result = scheduler.verify_single_credential(cred["id"], trigger_type="auto")
        cred = db.get_credential(cred["id"]) or cred

    push_event("vault:credentials_changed")
    return cred


def update_credential(
    credential_id: int,
    name: Optional[str] = None,
    group_id: Optional[int] = _UNSET,
    data: Optional[dict] = None,
    auto_verify: bool = False,
) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    # 显式传 None = 取消分组；省略（_UNSET）= 不修改分组
    if group_id is not _UNSET:
        kwargs["group_id"] = group_id
    if data is not None:
        cred = db.get_credential(credential_id)
        if not cred:
            return {"error": "not_found"}
        plugin_record = db.get_plugin_by_type(cred["credential_type"])
        sensitive_keys = set()
        if plugin_record:
            for f in plugin_record.get("fields_schema", []):
                if f.get("sensitive"):
                    sensitive_keys.add(f["key"])

        # 防御：敏感字段缺失或为空时保留已存明文，避免编辑表单未预填
        # 导致提交空值而覆盖密钥（凭据的敏感字段为空是无意义状态）。
        merged = dict(data)
        if sensitive_keys:
            try:
                existing_data = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                existing_data = {}
            for sk in sensitive_keys:
                if not merged.get(sk) and existing_data.get(sk):
                    merged[sk] = existing_data[sk]

        kwargs["data_encrypted"] = encrypt_dict(key, merged)
        kwargs["data_plain"] = {k: v for k, v in merged.items() if k not in sensitive_keys}

    updated = db.update_credential(credential_id, **kwargs)

    if auto_verify and data is not None:
        scheduler.verify_single_credential(credential_id, trigger_type="auto")
        updated = db.get_credential(credential_id) or updated

    push_event("vault:credentials_changed")
    return updated or {"error": "not_found"}


def delete_credential(credential_id: int) -> dict:
    db.delete_credential(credential_id)
    push_event("vault:credentials_changed")
    return {"ok": True}


def verify_credential(credential_id: int) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    return scheduler.verify_single_credential(credential_id, trigger_type="manual")


def batch_verify(
    scope: str = "all",
    group_id: Optional[int] = None,
    credential_type: Optional[str] = None,
) -> dict:
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    creds = db.list_credentials(
        group_id=group_id if scope == "group" else None,
        credential_type=credential_type if scope == "type" else None,
    )
    scheduler.batch_verify(scope=scope, group_id=group_id,
                           credential_type=credential_type, trigger_type="manual")
    return {"ok": True, "total": len(creds)}


def get_verification_history(credential_id: int, limit: int = 20) -> list:
    return db.get_verification_history(credential_id, limit)


# ── Groups ────────────────────────────────────────────────

def list_groups() -> list:
    return db.list_groups()


def create_group(name: str, description: str = "", color: str = "#409EFF") -> dict:
    return db.create_group(name, description, color)


def update_group(group_id: int, name: str = None, description: str = None, color: str = None) -> dict:
    kwargs = {k: v for k, v in {"name": name, "description": description, "color": color}.items() if v is not None}
    return db.update_group(group_id, **kwargs) or {"error": "not_found"}


def delete_group(group_id: int) -> dict:
    db.delete_group(group_id)
    return {"ok": True}


# ── Plugins ───────────────────────────────────────────────

def list_plugins() -> list:
    return plugin_manager.list_installed_plugins()


def install_plugin(zip_path: str) -> dict:
    ok, msg = plugin_manager.install_from_zip(zip_path)
    if ok:
        push_event("vault:plugin_installed", {"message": msg})
    return {"ok": ok, "message": msg}


def uninstall_plugin(credential_type: str) -> dict:
    ok, msg = plugin_manager.uninstall_plugin(credential_type)
    return {"ok": ok, "message": msg}


def toggle_plugin(credential_type: str, enabled: bool) -> dict:
    db.set_plugin_enabled(credential_type, enabled)
    if enabled:
        plugin_manager.reload_plugin(credential_type)
    return {"ok": True}


# ── MCP ───────────────────────────────────────────────────

def get_mcp_status() -> dict:
    running = mcp_server.is_running()
    port = mcp_server.get_port()
    return {
        "running": running,
        "port": port,
        "active_tokens": db.count_active_tokens(),
        "url": f"http://127.0.0.1:{port}/sse" if running else "",
    }


def start_mcp() -> dict:
    ok, port, url = mcp_server.start_server()
    tray.update_tray()
    push_event("vault:mcp_status", {"running": ok, "port": port, "active_tokens": db.count_active_tokens()})
    return {"ok": ok, "port": port, "url": url}


def stop_mcp() -> dict:
    mcp_server.stop_server()
    tray.update_tray()
    push_event("vault:mcp_status", {"running": False, "port": 0, "active_tokens": 0})
    return {"ok": True}


def list_mcp_permissions() -> list:
    return db.list_mcp_permissions()


def set_mcp_permission(tool_name: str, enabled: bool) -> dict:
    db.set_mcp_permission(tool_name, enabled)
    return {"ok": True}


def list_mcp_tokens() -> list:
    return db.list_mcp_tokens()


def create_mcp_token(
    name: str,
    tools_allowed: list = None,
    expires_days: Optional[int] = None,
) -> dict:
    token = generate_token()
    token_hash = hash_token(token)
    token_prefix = token[:8]
    expires_at = None
    if expires_days:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat()

    record = db.create_mcp_token(
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        tools_allowed=tools_allowed or [],
        expires_at=expires_at,
    )
    tray.update_tray()
    return {
        "id": record["id"],
        "name": record["name"],
        "token": token,          # 明文仅此一次
        "token_prefix": token_prefix,
    }


def revoke_mcp_token(token_id: int) -> dict:
    db.revoke_mcp_token(token_id)
    tray.update_tray()
    return {"ok": True}


def update_token_scope(token_id: int, tools_allowed: list) -> dict:
    db.update_token_scope(token_id, tools_allowed)
    return {"ok": True}


# ── Schedules ─────────────────────────────────────────────

def list_schedules() -> list:
    return db.list_schedules()


def create_schedule(
    name: str,
    trigger_type: str,
    trigger_args: dict,
    target_scope: str = "all",
    target_id: Optional[Union[int, str]] = None,
) -> dict:
    sched = db.create_schedule(name, trigger_type, trigger_args, target_scope, target_id)
    scheduler.reload_schedules()
    return sched


def update_schedule(schedule_id: int, payload: dict = None) -> dict:
    # pywebview 桥接仅支持位置参数，前端以 (id, payload_dict) 形式调用，
    # 因此这里接收字典再解包，而非使用 **kwargs（JS 无法触达）。
    db.update_schedule(schedule_id, **(payload or {}))
    scheduler.reload_schedules()
    return db.list_schedules()


def delete_schedule(schedule_id: int) -> dict:
    db.delete_schedule(schedule_id)
    scheduler.reload_schedules()
    return {"ok": True}


# ── Dashboard ─────────────────────────────────────────────

def get_dashboard_stats() -> dict:
    return db.get_dashboard_stats()


# ── Tray ──────────────────────────────────────────────────

def minimize_to_tray() -> dict:
    if _window:
        try:
            _window.hide()
            tray.show_first_minimize_bubble()
        except Exception as e:
            print(f"[api] 隐藏窗口失败: {e}")
    return {"ok": True}


def get_tray_status_api() -> dict:
    return get_tray_status()
