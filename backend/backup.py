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

# 仅备份/恢复这几项与主密码无关的偏好设置（salt / verify_token / mcp_* 不备份）
_PREF_KEYS = ("verify_proxy", "log_level", "close_action")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_keys(credential_type: str) -> set:
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return set()
    return {f["key"] for f in record.get("fields_schema", []) if f.get("sensitive")}


def _sensitive_values(credential_type: str, data: dict) -> tuple:
    return tuple(sorted(str(data[k]) for k in _sensitive_keys(credential_type) if data.get(k)))


def build_backup(key: bytes, scope: str = "all", encrypted: bool = True) -> dict:
    """构建备份文档。scope: 'all' | 'credentials'。"""
    groups = {g["id"]: g["name"] for g in db.list_groups()}
    entries: list[dict] = []
    for cred in db.list_credentials():
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
    if scope == "all":
        doc["groups"] = [
            {"name": g["name"], "description": g.get("description", ""), "color": g.get("color", "#409EFF")}
            for g in db.list_groups()
        ]
        doc["prefs"] = {k: db.get_setting(k, "") for k in _PREF_KEYS}
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
    }


def read_backup_file(path: str) -> Optional[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_backup_file(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
