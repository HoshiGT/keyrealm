"""
pywebview JS-API 桥接层
所有前端调用的方法都在这里定义
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional, Union

from . import backup
from . import database as db
from . import logger
from . import mcp_server
from . import plugin_manager
from . import preset_store
from . import proxy as proxy_config
from . import proxy_store
from . import scheduler
from . import tray
from . import verify_options
from .crypto import (
    check_verify_token,
    create_verify_token,
    decrypt_dict,
    derive_key,
    encrypt_dict,
    generate_salt,
    generate_token,
    hash_token,
    mask_secret,
)

# 内存中的 AES 密钥（解锁后存在，锁定后清除）
_master_key: Optional[bytes] = None
_key_lock = threading.Lock()

# 哨兵：区分“未提供该参数”与“显式传 None（如清除分组）”
_UNSET = object()

# pywebview window 引用（由 main.py 注入）
_window = None

# 本模块 logger
_log = logger.get_logger(__name__)


def set_window(window):
    global _window
    _window = window


# 关闭窗口行为回调（由 main.py 注入）：分别执行"直接退出"与"隐藏到托盘"
_close_quit_cb = None
_close_hide_cb = None


def set_close_callbacks(quit_cb=None, hide_cb=None):
    """注入关闭行为回调：quit_cb=直接退出，hide_cb=隐藏到托盘。"""
    global _close_quit_cb, _close_hide_cb
    _close_quit_cb = quit_cb
    _close_hide_cb = hide_cb


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
        _log.warning("事件推送失败 %s: %s", event_name, e)


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
            _log.error("重新加密失败 cred_id=%s: %s", cred["id"], e)

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
    credential_types: Optional[list] = None,
) -> list:
    creds = db.list_credentials(group_id=group_id, credential_type=credential_type,
                                status=status, search=search,
                                credential_types=credential_types or None)
    # 附加敏感字段掩码（如 sk-123…abcd），供列表页直接展示部分凭据信息。
    # 仅在保险库已解锁时计算；锁定时返回空掩码。
    key = _get_key()
    if key:
        schema_map = {p["credential_type"]: p for p in db.list_plugins()}
        for cred in creds:
            cred["data_masked"] = _masked_fields(key, cred, schema_map.get(cred["credential_type"]))
    return creds


def _masked_fields(key: bytes, cred: dict, plugin_record: Optional[dict]) -> dict:
    sensitive = [
        f["key"] for f in (plugin_record or {}).get("fields_schema", []) if f.get("sensitive")
    ]
    if not sensitive:
        return {}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        return {}
    return {k: mask_secret(str(data[k])) for k in sensitive if data.get(k)}


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

    # 类别/插件级默认设置先补进来——默认值可能正好满足必填项，所以必须在校验之前
    data = preset_store.apply_defaults(credential_type, data)

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
        scheduler.verify_single_credential(cred["id"], trigger_type="auto")
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
    credential_ids: Optional[list] = None,
    concurrency=None,
    timeout=None,
) -> dict:
    """触发批量验证（后台并发执行）。

    `concurrency` / `timeout` 来自「批量验证」对话框里的两个输入框：传了就先落库再执行，
    下次打开对话框沿用；不传则用已保存的设置。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    options = verify_options.save(concurrency, timeout)
    _log.info("批量验证参数：并发 %d，超时 %gs",
              options["concurrency"], options["timeout"])

    if credential_ids:
        ids = set(int(i) for i in credential_ids)
        creds = [c for c in db.list_credentials() if c.get("id") in ids]
    else:
        creds = db.list_credentials(
            group_id=group_id if scope == "group" else None,
            credential_type=credential_type if scope == "type" else None,
        )
    scheduler.batch_verify(scope=scope, group_id=group_id,
                           credential_type=credential_type, trigger_type="manual",
                           credential_ids=credential_ids,
                           concurrency=options["concurrency"])
    return {"ok": True, "total": len(creds),
            "concurrency": options["concurrency"], "timeout": options["timeout"]}


def get_batch_options() -> dict:
    """批量验证的运行参数（并发线程数 / 请求超时）与取值范围，供对话框回填。"""
    return {"ok": True, **verify_options.get_options()}



def check_duplicate_credential(credential_type: str, data: dict) -> list:
    """检测与新增数据重复的既有凭据：任一敏感字段值与既有凭据
    解密后任意字符串字段值相同即视为重复。返回匹配项摘要列表。"""
    key = _get_key()
    if key is None:
        return []

    plugin_record = db.get_plugin_by_type(credential_type)
    sensitive_keys = set()
    if plugin_record:
        for f in plugin_record.get("fields_schema", []):
            if f.get("sensitive"):
                sensitive_keys.add(f["key"])
    new_values = {str(data[k]).strip() for k in sensitive_keys if data.get(k)}
    if not new_values:
        return []

    matches = []
    for cred in db.list_credentials():
        try:
            existing = decrypt_dict(key, cred["data_encrypted"])
        except Exception:
            continue
        existing_values = {str(v).strip() for v in existing.values() if isinstance(v, str) and v}
        if new_values & existing_values:
            matches.append({
                "id": cred["id"],
                "name": cred["name"],
                "credential_type": cred["credential_type"],
            })
    return matches


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
    plugins = plugin_manager.list_installed_plugins()
    for record in plugins:
        record["icon_image"] = plugin_manager.get_icon_image(record)
        record["batch_verify"] = bool(record.get("batch_verify", 1))
        record["has_actions"] = plugin_manager.has_actions(record["credential_type"])
    return plugins


def credential_capabilities(credential_id: int) -> dict:
    """该凭据当前**真正能用**的操作（插件可选能力 `capabilities`）。

    与 `list_credential_actions` 分开是有意的：`actions()` 必须便宜且无副作用（菜单靠它渲染），
    而权限探测要联网、可能失败。分开了才能把三种状态说清楚：
    `actions 为空`（没有操作）/ `available 为空`（有操作但没权限）/ `探测失败`（全都展示）。
    """
    key = _get_key()
    if key is None:
        return {"available": None, "note": "", "error": True}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"available": None, "note": "", "error": True}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_capabilities(cred["credential_type"], data)


def pick_save_path(default_name: str = "", file_types: Optional[list] = None) -> dict:
    """弹系统「另存为」，返回用户选的路径。

    下载类操作（仓库打包、对象下载）**必须先经过这一步**：插件自己不该弹对话框
    （流式路径上插件跑在新建线程里，跨线程弹窗会卡界面），也不能把带鉴权头的下载 URL
    回传给前端——那等于把短期凭证交给 JS。正确顺序是：前端先拿到路径 → 路径塞进 params → 插件自己下载。
    """
    safe_name = _safe_filename(default_name)
    path = _file_dialog("save", default_name=safe_name, file_types=file_types)
    if not path:
        return {"ok": False, "message": "已取消"}
    return {"ok": True, "path": path}


def _safe_filename(name: str) -> str:
    """把用户/插件给的默认文件名洗干净：路径分隔符与控制字符不能进文件名。"""
    text = str(name or "").strip()
    for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        text = text.replace(ch, "_")
    text = text.replace("..", "_")
    return text[:120]


def list_credential_actions(credential_id: int) -> list:
    """某凭据对应插件声明的「简易操作」列表（可选能力）。"""
    key = _get_key()
    if key is None:
        return []
    cred = db.get_credential(credential_id)
    if not cred:
        return []
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_actions(cred["credential_type"], data)


def _credential_secrets(credential_type: str, data: dict) -> list:
    """返回该凭据所有敏感字段的明文值（用于输出净化）。"""
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return []
    values = []
    for field in record.get("fields_schema", []):
        key = field.get("key")
        if field.get("sensitive") and data.get(key):
            values.append(str(data[key]))
    return values


def _redact_secrets(text: Any, secrets: list) -> Any:
    """把文本里出现的敏感值替换成 ***（长度 ≥ 6 才替换，避免误伤短串）。"""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return out


def _redact_deep(value: Any, secrets: list) -> Any:
    """递归净化整个结果。

    **不能只扫顶层的 ``output`` / ``error``**：列表类操作把数据放在 ``rows`` / ``columns`` 里，
    只扫顶层等于让列表里回显的密钥原样穿到前端（JS 桥那一侧没有任何净化）。
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return _redact_secrets(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_deep(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, secrets) for v in value]
    return value


def _declared_fields(credential_type: str) -> set[str]:
    """该插件在 manifest 里声明过的字段 key（用于限定「操作写回」能改哪些字段）。"""
    record = db.get_plugin_by_type(credential_type) or {}
    schema = record.get("fields_schema") or []
    return {str(f.get("key")) for f in schema if isinstance(f, dict) and f.get("key")}


def run_credential_action(credential_id: int, action_id: str, params: Optional[dict] = None) -> dict:
    """执行某凭据的插件操作（输出/错误统一做密钥净化）。

    插件可在结果里回传 ``updated_data``（``{字段key: 新值}``）把新值**写回本凭据**——
    用于「用 refresh_token 换一组新令牌」这类必须原子保存的操作（换令牌会轮换 refresh_token，
    不写回就等于把用户本机的登录态搞坏）。约束：
      - 只接受**该插件声明过**的字段，其它键一律丢弃（免得插件往库里塞野字段）；
      - ``updated_data`` 本身**绝不回给前端**（里面是新明文令牌），只回一个 ``data_updated``
        字段名列表，让界面刷新即可。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"ok": False, "error": "凭据不存在"}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    result = plugin_manager.run_plugin_action(
        cred["credential_type"], action_id, params or {}, data
    )
    # 写回：先摘掉 updated_data，再按声明字段过滤合并，最后重加密落库。
    updated = result.pop("updated_data", None) if isinstance(result, dict) else None
    if isinstance(updated, dict) and updated:
        declared = _declared_fields(cred["credential_type"])
        patch = {k: v for k, v in updated.items() if k in declared}
        if patch:
            merged = {**data, **patch}
            try:
                db.update_credential(credential_id, data_encrypted=encrypt_dict(key, merged))
                data = merged
                result["data_updated"] = sorted(patch)
                _log.info("操作 %s/%s 写回凭据 %s 字段：%s",
                          cred["credential_type"], action_id, credential_id, sorted(patch))
            except Exception as e:  # noqa: BLE001 - 写回失败不能吞掉操作本身的结论
                _log.exception("操作写回凭据失败 %s", credential_id)
                result["data_updated"] = []
                result["data_update_error"] = type(e).__name__
    secrets = _credential_secrets(cred["credential_type"], data)
    # 整份结果递归净化：列表类操作的 rows/columns 也在这里面
    result = _redact_deep(result, secrets)
    # 日志同样必须净化：插件回显的密钥不能落盘到日志文件
    ok = bool(isinstance(result, dict) and result.get("ok"))
    _log.info(
        "执行操作 %s/%s [%s]：%s",
        cred["credential_type"], action_id, cred.get("name", ""),
        "成功" if ok else "失败",
    )
    if not ok and isinstance(result, dict) and result.get("error"):
        _log.warning("操作 %s/%s 失败原因: %s", cred["credential_type"], action_id,
                     _redact_secrets(str(result["error"]), secrets))
    return result


def list_credential_models(credential_id: int) -> list:
    """某凭据对应插件可用的模型列表（供 LLM「简单对话」选择模型）。"""
    key = _get_key()
    if key is None:
        return []
    cred = db.get_credential(credential_id)
    if not cred:
        return []
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}
    return plugin_manager.plugin_models(cred["credential_type"], data)


def list_draft_models(credential_type: str, data: Optional[dict] = None) -> list:
    """尚未保存的（新增/编辑表单里的）凭据可用的模型列表。

    与 ``list_credential_models`` 的区别是数据来自表单而非库，因此新建凭据时也能
    拿到下拉候选；失败时插件返回空数组，前端回退为自由输入。
    """
    if not credential_type:
        return []
    return plugin_manager.plugin_models(credential_type, data or {})


def stream_credential_action(credential_id: int, action_id: str, params: Optional[dict] = None) -> dict:
    """流式执行某凭据的插件操作：后台线程边收边推送 vault:action_stream 事件。

    事件 detail：
      - {"stream_id": ..., "delta": "…"}                        逐段增量
      - {"stream_id": ..., "done": true, "ok": .., "output": .., "error": ..}  结束
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    cred = db.get_credential(credential_id)
    if not cred:
        return {"ok": False, "error": "凭据不存在"}
    try:
        data = decrypt_dict(key, cred["data_encrypted"])
    except Exception:
        data = {}

    stream_id = str(uuid.uuid4())
    credential_type = cred["credential_type"]
    secrets = _credential_secrets(credential_type, data)

    def _run():
        def on_delta(text: str) -> None:
            push_event("vault:action_stream", {
                "stream_id": stream_id,
                "delta": _redact_secrets(text, secrets),
            })

        def on_progress(current, total, text: str = "") -> None:
            """下载/打包这类长操作的进度：``total <= 0`` 表示总量未知（前端只显示已完成的量）。

            进度是**尽力而为**——数值先夹到合法区间，推送本身出错也绝不能把操作搞失败。
            """
            try:
                cur = max(0, int(current))
                tot = max(0, int(total or 0))
            except (TypeError, ValueError):
                return
            push_event("vault:action_stream", {
                "stream_id": stream_id,
                "progress": {
                    "current": cur,
                    "total": tot,
                    "text": _redact_secrets(str(text or ""), secrets),
                },
            })

        result = plugin_manager.stream_plugin_action(
            credential_type, action_id, params or {}, data, on_delta, on_progress
        )
        safe_output = _redact_secrets(result.get("output", ""), secrets)
        safe_error = _redact_secrets(result.get("error", ""), secrets)
        push_event("vault:action_stream", {
            "stream_id": stream_id,
            "done": True,
            "ok": bool(result.get("ok")),
            "output": safe_output,
            "error": safe_error,
        })
        ok = bool(result.get("ok"))
        _log.info("流式操作 %s/%s [%s]：%s", credential_type, action_id,
                  cred.get("name", ""), "成功" if ok else "失败")
        if not ok and safe_error:
            _log.warning("流式操作 %s/%s 失败原因: %s", credential_type, action_id, safe_error)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "stream_id": stream_id}


def delete_plugin(credential_type: str) -> dict:
    """删除插件及其凭据（前端确认后调用）。"""
    result = plugin_manager.delete_plugin(credential_type, delete_credentials=True)
    if result.get("ok"):
        push_event("vault:credentials_changed")
        push_event("vault:plugins_changed")
    return result


def list_available_plugins() -> list:
    """内置源中尚未安装的插件。"""
    available = plugin_manager.list_available_plugins()
    for item in available:
        item["icon_image"] = plugin_manager.get_icon_image({
            "icon": item.get("icon", ""),
            "credential_type": item["credential_type"],
            "plugin_path": str(plugin_manager.BUILTIN_SOURCE_DIR / item["category"] / item["service"]),
        })
    return available


def install_plugin(category: str, service: str) -> dict:
    """从内置源恢复插件并加载。"""
    result = plugin_manager.install_plugin(category, service)
    if result.get("ok"):
        push_event("vault:plugins_changed")
    return result


# ── MCP ───────────────────────────────────────────────────

MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 8765


def _mcp_host() -> str:
    return db.get_setting("mcp_host", "").strip() or MCP_DEFAULT_HOST


def _mcp_port() -> int:
    try:
        port = int(db.get_setting("mcp_port", "").strip())
    except (TypeError, ValueError):
        return MCP_DEFAULT_PORT
    return port if 1 <= port <= 65535 else MCP_DEFAULT_PORT


def _valid_mcp_host(host: str) -> bool:
    """只接受合法 IP 或 localhost —— 别让人一不小心把凭据服务挂到公网。"""
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _wait_mcp_stopped(timeout: float = 3.0) -> bool:
    """等服务真正停下来（uvicorn 退出后端口才释放，紧接着启动会撞上自己）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mcp_server.is_running():
            return True
        time.sleep(0.1)
    return not mcp_server.is_running()


def get_mcp_status() -> dict:
    running = mcp_server.is_running()
    return {
        "running": running,
        "host": mcp_server.get_host(),
        "port": mcp_server.get_port(),
        "active_tokens": db.count_active_tokens(),
        "url": mcp_server.get_url() if running else "",
    }


def get_mcp_options() -> dict:
    """MCP 监听地址（供设置页回填）。"""
    return {"ok": True, "host": _mcp_host(), "port": _mcp_port(),
            "defaults": {"host": MCP_DEFAULT_HOST, "port": MCP_DEFAULT_PORT},
            "running": mcp_server.is_running()}


def set_mcp_options(host: str, port) -> dict:
    """保存监听地址；服务在运行则按新地址重启。

    端口被占用时**不改动已保存的值**，并把原地址的服务起回去——不能把用户留在「改完啥都没了」的状态。
    """
    host = (host or "").strip() or MCP_DEFAULT_HOST
    if not _valid_mcp_host(host):
        return {"ok": False, "message": f"监听地址不合法：{host}（只允许 IP 或 localhost）"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "message": "端口必须是数字"}
    if not (1 <= port <= 65535):
        return {"ok": False, "message": "端口需在 1–65535 之间"}

    old_host, old_port = _mcp_host(), _mcp_port()
    was_running = mcp_server.is_running()
    if was_running:
        mcp_server.stop_server()
        _wait_mcp_stopped()

    if was_running:
        ok, actual_port, url = mcp_server.start_server(port, host)
        if not ok:
            mcp_server.start_server(old_port, old_host)   # 回滚：服务照旧跑着
            _push_mcp_status()
            return {"ok": False, "message": f"端口 {port} 已被占用，配置未保存（服务已按原地址恢复）"}
    else:
        actual_port, url = port, ""

    db.set_setting("mcp_host", host)
    db.set_setting("mcp_port", str(port))
    _push_mcp_status()
    _log.info("MCP 监听地址已更新为 %s:%s", host, port)
    return {"ok": True, "host": host, "port": actual_port, "restarted": was_running,
            "url": url or f"http://{host}:{port}/sse"}


def start_mcp() -> dict:
    host, port = _mcp_host(), _mcp_port()
    ok, actual_port, url = mcp_server.start_server(port, host)
    _push_mcp_status()
    if not ok:
        return {"ok": False, "message": f"端口 {port} 已被占用，请到设置里换一个端口"}
    return {"ok": True, "host": host, "port": actual_port, "url": url}


def stop_mcp() -> dict:
    mcp_server.stop_server()
    _push_mcp_status()
    return {"ok": True}


def list_mcp_permissions() -> list:
    return db.list_mcp_permissions()


def set_mcp_permission(tool_name: str, enabled: bool) -> dict:
    db.set_mcp_permission(tool_name, enabled)
    return {"ok": True}


def _new_mcp_token() -> dict:
    """生成并落库一个新的 MCP Token。工具范围统一继承「工具权限」全局开关，默认永不过期。"""
    token = generate_token()
    token_prefix = token[:8]
    record = db.create_mcp_token(
        name="default",
        token_hash=hash_token(token),
        token_prefix=token_prefix,
        tools_allowed=[],
        expires_at=None,
    )
    return {"record": record, "token": token, "token_prefix": token_prefix}


def ensure_mcp_token() -> Optional[dict]:
    """确保存在一个活跃 Token（无则创建），返回活跃 Token 记录（不含明文）。"""
    record = db.get_active_mcp_token()
    if record:
        return record
    return _new_mcp_token()["record"]


def get_mcp_token() -> dict:
    """返回当前活跃 Token 的元数据（不含明文）；无则自动创建一个。"""
    record = ensure_mcp_token()
    if not record:
        return {"exists": False}
    return {
        "exists": True,
        "id": record["id"],
        "token_prefix": record["token_prefix"],
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
    }


def _push_mcp_status():
    """推送 MCP 状态变化事件（供前端与托盘刷新）。"""
    tray.update_tray()
    push_event("vault:mcp_status", {
        "running": mcp_server.is_running(),
        "host": mcp_server.get_host(),
        "port": mcp_server.get_port(),
        "active_tokens": db.count_active_tokens(),
    })


def rotate_mcp_token() -> dict:
    """轮换 MCP Token：撤销现有活跃 Token 并签发新的（明文仅返回一次）。"""
    db.revoke_active_mcp_tokens()
    created = _new_mcp_token()
    _push_mcp_status()
    _log.info("已轮换 MCP Token（前缀 %s…）", created["token_prefix"])
    return {
        "id": created["record"]["id"],
        "token": created["token"],          # 明文仅此一次
        "token_prefix": created["token_prefix"],
    }


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


# ── Tray ──────────────────────────────────────────────────

def minimize_to_tray() -> dict:
    if _window:
        try:
            _window.hide()
            tray.show_first_minimize_bubble()
        except Exception as e:
            _log.warning("隐藏窗口失败: %s", e)
    return {"ok": True}


def get_tray_status_api() -> dict:
    return get_tray_status()


# ── 关闭窗口行为 ──────────────────────────────────────────
# 用户点击窗口关闭按钮时可选择：每次询问 / 直接退出 / 后台运行（最小化到托盘）。
# 选择持久化于 settings 表键 close_action，可在"系统设置"中二次调整。

CLOSE_ACTIONS = ("ask", "quit", "background")
_CLOSE_ACTION_DEFAULT = "ask"
_CLOSE_ACTION_OPTIONS = [
    {"value": "ask", "label": "每次询问我"},
    {"value": "quit", "label": "直接退出程序"},
    {"value": "background", "label": "后台运行（最小化到托盘）"},
]


def _read_close_action() -> str:
    action = (db.get_setting("close_action", _CLOSE_ACTION_DEFAULT) or "").strip().lower()
    return action if action in CLOSE_ACTIONS else _CLOSE_ACTION_DEFAULT


def get_close_action() -> dict:
    """返回当前关闭行为与可选值（供"系统设置"展示）。"""
    return {"action": _read_close_action(), "options": _CLOSE_ACTION_OPTIONS}


def set_close_action(action: str) -> dict:
    """持久化关闭行为设置。"""
    action = (action or "").strip().lower()
    if action not in CLOSE_ACTIONS:
        return {"ok": False, "message": f"无效的关闭行为：{action or '(空)'}"}
    db.set_setting("close_action", action)
    _log.info("关闭窗口行为已设置为 %s", action)
    return {"ok": True, "action": action}


def confirm_close_action(action: str, remember: bool = False) -> dict:
    """前端在关闭确认弹窗中做出选择后回调（action: 'quit' | 'background'）。

    remember 为真时把该选择记为默认（下次不再询问）。
    """
    action = (action or "").strip().lower()
    if action not in ("quit", "background"):
        return {"ok": False, "message": f"无效的选择：{action or '(空)'}"}
    if remember:
        db.set_setting("close_action", action)
        _log.info("已记住关闭窗口行为：%s", action)
    if action == "quit":
        if _close_quit_cb:
            _close_quit_cb()
    else:
        if _close_hide_cb:
            _close_hide_cb()
    return {"ok": True, "action": action}


# ── 验证代理（注册表 + 全局默认） ───────────────────────────
# 一律按 id 引用；取名字只用于展示。每条凭据用哪个代理由凭据自己的字段决定，见 proxy_store。

def list_proxies() -> dict:
    """全部代理 + 当前全局默认，供「系统设置」渲染。"""
    return {
        "ok": True,
        "items": proxy_store.list_proxies(),
        "global_id": proxy_store.global_proxy_id(),
        "global_url": proxy_store.global_url() or "",
    }


def _apply_global_after_change() -> None:
    """代理表有变动后，按当前全局设置重新写一次环境变量（删掉的那个可能正被全局引用）。"""
    proxy_config.apply_proxy(proxy_store.global_url() or "")


def create_proxy(name: str, url: str) -> dict:
    result = proxy_store.create_proxy(name, url)
    if result.get("error"):
        return {"ok": False, "message": result.get("message") or "新增代理失败"}
    _log.info("已新增代理 #%s（%s）", result.get("id"), name)
    return {"ok": True, **list_proxies()}


def update_proxy(proxy_id: int, name=None, url=None) -> dict:
    result = proxy_store.update_proxy(proxy_id, name, url)
    if result.get("error"):
        return {"ok": False, "message": result.get("message") or "修改代理失败"}
    _apply_global_after_change()
    _log.info("已修改代理 #%s", proxy_id)
    return {"ok": True, **list_proxies()}


def delete_proxy(proxy_id: int) -> dict:
    result = proxy_store.delete_proxy(proxy_id)
    if not result.get("ok"):
        return {"ok": False, "message": "代理不存在"}
    _apply_global_after_change()
    _log.info("已删除代理 #%s", proxy_id)
    return {"ok": True, **list_proxies()}


def set_global_proxy(proxy_id=None) -> dict:
    """设置全局默认代理；``None`` / ``0`` / 空串 = 直连。"""
    if proxy_id in (None, "", 0, "0"):
        result = proxy_store.set_global_proxy(None)
        _log.info("全局代理已设为直连")
    else:
        result = proxy_store.set_global_proxy(int(proxy_id))
        if not result.get("ok"):
            return {"ok": False, "message": result.get("message") or "设置失败"}
        _log.info("全局代理已设为 #%s", proxy_id)
    return {"ok": True, **list_proxies()}


def test_proxy_id(proxy_id=None) -> dict:
    """对照测试某个代理（``None`` = 当前全局生效的那个）。"""
    result = proxy_store.test_proxy_by_id(proxy_id)
    verdict = result.get("verdict", {})
    _log.info("代理测试完成（%s）：%s", result.get("proxy_source", ""), verdict.get("headline", ""))
    return result


# ── 类别级 / 插件级默认设置 ─────────────────────────────────
# 框架只提供「存储 + 继承 + 注入」；哪些字段可预设由插件在 manifest 里自行声明
# （默认全部非敏感字段可预设，加 "preset": false 可排除）。敏感字段永远不可预设。

def get_credential_defaults(credential_type: str) -> dict:
    """该插件新建凭据时应带的默认值（插件级覆盖类别级）。新增表单打开时调它。"""
    return {
        "ok": True,
        "values": preset_store.defaults_for(credential_type),
        "uses_proxy": plugin_manager.plugin_uses_proxy(credential_type),
        # 给「使用代理」下拉里那项做括注：该插件选「跟随上层设置」时实际会走哪个出口
        "inherited_proxy": proxy_store.describe_inherited(credential_type),
    }


def get_scope_options(scope: str, scope_key: str, credential_type: str = "") -> dict:
    """某个作用域的默认设置界面所需数据（当前值 + 可预设字段列表）。"""
    data = preset_store.scope_options(scope, scope_key, credential_type)
    # 该作用域下有没有「走代理」的插件——没有就不给用户显示代理项，免得白设
    if scope == preset_store.SCOPE_PLUGIN:
        data["uses_proxy"] = plugin_manager.plugin_uses_proxy(credential_type or scope_key)
    else:
        data["uses_proxy"] = any(
            plugin_manager.plugin_uses_proxy(p["credential_type"])
            for p in db.list_plugins()
            if (p.get("category") or "") == scope_key
        )
    return {"ok": True, **data}


def set_scope_options(scope: str, scope_key: str, values: dict,
                      credential_type: str = "") -> dict:
    result = preset_store.set_scope_defaults(scope, scope_key, values or {}, credential_type)
    if result.get("ok"):
        _log.info("已保存 %s 默认设置 %s（%d 项）", scope, scope_key,
                  len(result.get("values") or {}))
    return result


def clear_scope_options(scope: str, scope_key: str) -> dict:
    _log.info("已清空 %s 默认设置 %s", scope, scope_key)
    return preset_store.clear_scope_defaults(scope, scope_key)


# ── 备份 / 恢复 ────────────────────────────────────────────

def _file_dialog(kind: str, default_name: str = "",
                 file_types: Optional[list] = None) -> Optional[str]:
    """打开系统文件对话框，返回所选路径（取消或无窗口时返回 None）。

    ``file_types`` 默认只给 JSON（备份用）；通用的「另存为」（仓库打包、对象下载）要自己传，
    否则用户面对一个写死的 `*.json` 过滤器会存错扩展名。
    """
    if _window is None:
        return None
    try:
        import webview

        # pywebview 5.x 起把 webview.SAVE_DIALOG / OPEN_DIALOG 收敛成了
        # webview.FileDialog 枚举，旧的模块级常量仍能用但会打印弃用警告；
        # 这里优先用新枚举，旧版本回退到常量。
        dialog_enum = getattr(webview, "FileDialog", None)
        if dialog_enum is not None:
            dialog_type = dialog_enum.SAVE if kind == "save" else dialog_enum.OPEN
        else:
            dialog_type = webview.SAVE_DIALOG if kind == "save" else webview.OPEN_DIALOG

        kwargs: dict = {"file_types": tuple(file_types or ("JSON 备份 (*.json)", "所有文件 (*.*)"))}
        if kind == "save":
            kwargs["save_filename"] = default_name
        result = _window.create_file_dialog(dialog_type, **kwargs)
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result
    except Exception as e:
        _log.warning("文件对话框打开失败: %s", e)
        return None


def build_backup_doc(scope: str = "all", encrypted: bool = True,
                     credential_ids: Optional[list] = None) -> dict:
    """构建备份文档（不弹对话框、不落盘）——GUI 导出与 MCP 工具共用。

    返回 ``{"ok": True, "document": {...}}``，或 ``{"error": "vault_locked"}`` /
    ``{"ok": False, "message": ...}``。
    """
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    if scope not in ("all", "credentials"):
        return {"ok": False, "message": f"无效范围：{scope}"}
    if credential_ids is not None and not credential_ids:
        return {"ok": False, "message": "未选择任何凭据"}

    doc = backup.build_backup(key, scope=scope, encrypted=encrypted,
                              credential_ids=credential_ids)
    return {"ok": True, "document": doc}


def apply_backup_doc(doc: dict, dedupe: bool = True) -> dict:
    """把备份文档并入当前库（不弹对话框）——GUI 导入与 MCP 工具共用。"""
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}
    try:
        result = backup.import_backup(key, doc, dedupe=dedupe)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    # 备份里的代理设置若被导入，立即生效（全局默认可能是按 id 引用的，要用解析后的地址）
    proxy_config.apply_proxy(proxy_store.global_url() or "")
    push_event("vault:credentials_changed")
    push_event("vault:plugins_changed")
    _log.info("已导入备份：导入 %d 条", result["imported"])
    return result


def export_backup(scope: str = "all", encrypted: bool = True,
                  credential_ids: Optional[list] = None) -> dict:
    """导出备份到用户选择的文件。

    - ``encrypted=True``：敏感值沿用密文（需同一主密码导入）
    - ``credential_ids``：只导出这些凭据（省略或 None = 全部）
    """
    built = build_backup_doc(scope, encrypted, credential_ids)
    if built.get("error") or built.get("ok") is False:
        return built
    doc = built["document"]

    path = _file_dialog(
        "save", default_name=f"credential-vault-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    if not path:
        return {"ok": False, "message": "已取消"}

    try:
        backup.write_backup_file(path, doc)
    except Exception as e:
        return {"ok": False, "message": f"写入失败: {e}"}

    _log.info("已导出备份 %s（%d 条凭据，范围=%s，加密=%s）",
              path, doc["counts"]["credentials"], scope, encrypted)
    return {
        "ok": True,
        "path": path,
        "count": doc["counts"]["credentials"],
        "scope": scope,
        "selected": credential_ids is not None,
        "encrypted": bool(encrypted),
    }


def import_backup() -> dict:
    """从用户选择的备份文件导入（自动识别加密/明文）。"""
    key = _get_key()
    if key is None:
        return {"error": "vault_locked"}

    path = _file_dialog("open")
    if not path:
        return {"ok": False, "message": "已取消"}

    try:
        doc = backup.read_backup_file(path)
    except Exception as e:
        return {"ok": False, "message": f"读取失败: {e}"}

    result = apply_backup_doc(doc)
    if result.get("ok"):
        result["path"] = path
    return result


# ── Logging ───────────────────────────────────────────────

def get_log_config() -> dict:
    """返回当前日志级别、可选级别列表与日志文件路径。"""
    return {
        "level": logger.get_level(),
        "levels": logger.LEVELS,
        "log_file": logger.log_file_path(),
    }


def set_log_level(level: str) -> dict:
    """设置日志级别：立即生效并持久化到 settings（下次启动恢复）。"""
    level = (level or "").strip().upper()
    if level not in logger.LEVELS:
        return {"ok": False, "message": f"无效日志级别：{level or '(空)'}"}
    logger.set_level(level)
    db.set_setting("log_level", level)
    _log.info("日志级别已设置为 %s", level)
    return {"ok": True, "level": level}


def get_logs(limit: int = 200, level: str = "", search: str = "") -> dict:
    """读取日志文件末尾若干条记录，可按最低级别过滤 + 关键词搜索。"""
    try:
        limit = max(1, min(int(limit or 200), 2000))
    except (TypeError, ValueError):
        limit = 200
    level = (level or "").strip().upper()
    if level and level not in logger.LEVELS:
        level = ""
    search = (search or "").strip()
    entries = logger.read_logs(limit, level or None, search or None)
    return {
        "ok": True,
        "entries": entries,
        "count": len(entries),
        "log_file": logger.log_file_path(),
    }


def clear_logs() -> dict:
    """清空日志文件与轮转备份。"""
    logger.clear_logs()
    _log.info("日志已清空")
    return {"ok": True, "message": "日志已清空"}
