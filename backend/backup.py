"""备份 / 恢复：导出为单个 JSON 文件，可选加密（默认）或明文。

- **加密导出**：敏感字段直接沿用库中密文（`data_encrypted`），全程不落明文，
  但只能用**同一主密码**导入。
- **明文导出**：敏感字段以明文写入文件（敏感，慎用），可在任意主密码的库上导入。
- **导入**：自动识别 `encrypted` 字段；加密备份需与当前主密码匹配，解密后以当前
  主密钥重新加密写库。未安装插件的凭据会被跳过（不导入"孤儿"）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import database as db
from .crypto import decrypt_dict, encrypt_dict

FORMAT = "credential-vault-backup"
VERSION = 1

# 仅备份/恢复这几项与主密码、与本机环境无关的偏好设置
# （salt / verify_token 不备份；mcp_host / mcp_port 是本机监听配置，换台机器未必可用，不跟随备份）
_PREF_KEYS = ("verify_proxy", "global_proxy_id", "log_level", "close_action")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_keys(credential_type: str) -> set:
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return set()
    return {f["key"] for f in record.get("fields_schema", []) if f.get("sensitive")}


def _sensitive_values(credential_type: str, data: dict) -> tuple:
    return tuple(sorted(str(data[k]) for k in _sensitive_keys(credential_type) if data.get(k)))


def build_backup(
    key: bytes,
    scope: str = "all",
    encrypted: bool = True,
    credential_ids: Optional[list] = None,
) -> dict:
    """构建备份文档。

    - ``scope``：``'all'``（凭据 + 分组 + 偏好设置）/ ``'credentials'``（只有凭据）
    - ``credential_ids``：只导出这些凭据（``None`` = 全部）。**不影响** scope 的语义，
      所以「全部 + 选中若干」与「仅凭据 + 选中若干」的差别仍然只是分组/偏好设置。
    """
    groups = {g["id"]: g["name"] for g in db.list_groups()}
    selected: Optional[set] = None
    if credential_ids is not None:
        selected = {int(cid) for cid in credential_ids}

    entries: list[dict] = []
    for cred in db.list_credentials():
        if selected is not None and cred["id"] not in selected:
            continue
        entry: dict[str, Any] = {
            "name": cred["name"],
            "credential_type": cred["credential_type"],
            "group": groups.get(cred["group_id"]),
            "data_plain": cred.get("data_plain") or {},
        }
        if encrypted:
            entry["data_encrypted"] = cred["data_encrypted"]
        else:
            try:
                entry["data"] = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                entry["data"] = {}
        entries.append(entry)

    doc: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": _iso(),
        "scope": scope,
        "encrypted": bool(encrypted),
        "counts": {"credentials": len(entries)},
        "credentials": entries,
    }
    if selected is not None:
        doc["credential_ids"] = sorted(selected)
    if scope == "all":
        doc["groups"] = [
            {"name": g["name"], "description": g.get("description", ""), "color": g.get("color", "#409EFF")}
            for g in db.list_groups()
        ]
        doc["prefs"] = {k: db.get_setting(k, "") for k in _PREF_KEYS}
        # 代理注册表与类别/插件默认设置（只存名字与地址，不存 id —— id 是各库自增的）
        doc["proxies"] = [{"name": p["name"], "url": p["url"]} for p in db.list_proxies()]
        doc["preset_defaults"] = [
            {"scope": r["scope"], "scope_key": r["scope_key"], "values": r.get("payload") or "{}"}
            for r in db.list_preset_defaults()
        ]
    return doc


def import_backup(key: bytes, doc: dict, dedupe: bool = True) -> dict:
    """把备份文档导入当前库。加密备份需与当前主密码匹配。"""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("不是有效的 Credential Vault 备份文件")

    encrypted = bool(doc.get("encrypted"))
    entries = doc.get("credentials") or []

    # 1) 分组：按名称补建
    created_groups = 0
    name_to_id = {g["name"]: g["id"] for g in db.list_groups()}
    for g in doc.get("groups") or []:
        name = (g.get("name") or "").strip()
        if name and name not in name_to_id:
            ng = db.create_group(name, g.get("description", ""), g.get("color", "#409EFF"))
            name_to_id[name] = ng["id"]
            created_groups += 1

    # 1.5) 代理注册表与默认设置（仅整库备份带这两个 section）
    #      代理按「地址」去重、**不还原 id**：凭据里引用的 _proxy_id 是各库自增的，
    #      跨库搬过来必然对不上——那些凭据的代理会在解析时回退到全局（不报错）。
    created_proxies = 0
    known_urls = {p["url"] for p in db.list_proxies()}
    for item in doc.get("proxies") or []:
        url = str(item.get("url") or "").strip()
        if url and url not in known_urls:
            db.create_proxy(str(item.get("name") or url), url)
            known_urls.add(url)
            created_proxies += 1

    for item in doc.get("preset_defaults") or []:
        scope = item.get("scope")
        scope_key = item.get("scope_key")
        if scope in ("category", "plugin") and scope_key:
            try:
                values = json.loads(item.get("values") or "{}")
            except Exception:
                continue
            if isinstance(values, dict):
                db.set_preset_defaults(scope, scope_key, values)

    # 2) 现有凭据的敏感值索引（去重）
    existing: dict[str, set] = {}
    if dedupe:
        for cred in db.list_credentials():
            try:
                data = decrypt_dict(key, cred["data_encrypted"])
            except Exception:
                continue
            existing.setdefault(cred["credential_type"], set()).add(
                _sensitive_values(cred["credential_type"], data)
            )

    imported = skipped_plugin = skipped_dup = 0
    for row in entries:
        ct = row.get("credential_type") or ""
        if not db.get_plugin_by_type(ct):
            skipped_plugin += 1
            continue

        if encrypted:
            blob = row.get("data_encrypted")
            if not blob:
                skipped_plugin += 1
                continue
            try:
                data = decrypt_dict(key, blob)
            except Exception:
                raise ValueError("备份是加密的，且与当前主密码不匹配，无法导入")
        else:
            data = row.get("data") or {}

        values = _sensitive_values(ct, data)
        if dedupe and values and values in existing.get(ct, set()):
            skipped_dup += 1
            continue

        sensitive = _sensitive_keys(ct)
        db.create_credential(
            name=row.get("name") or "（导入）",
            credential_type=ct,
            data_encrypted=encrypt_dict(key, data),
            data_plain={k: v for k, v in data.items() if k not in sensitive},
            group_id=name_to_id.get(row.get("group") or ""),
        )
        if dedupe and values:
            existing.setdefault(ct, set()).add(values)
        imported += 1

    # 3) 偏好设置（仅整库范围）
    if doc.get("scope") == "all" and isinstance(doc.get("prefs"), dict):
        for k in _PREF_KEYS:
            if k in doc["prefs"]:
                db.set_setting(k, str(doc["prefs"][k]))

    return {
        "ok": True,
        "encrypted": encrypted,
        "imported": imported,
        "skipped_plugin": skipped_plugin,
        "skipped_duplicate": skipped_dup,
        "created_groups": created_groups,
        "created_proxies": created_proxies,
    }


def read_backup_file(path: str) -> Optional[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_backup_file(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
