"""
集成测试：MCP 双层权限（全局开关 ∩ Token 范围）+ ContextVar 传播
运行：python -m pytest tests/test_mcp_permissions.py -v
或直接：python tests/test_mcp_permissions.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database as db
from backend import crypto
from backend.mcp_server import _check_permission, _current_token_var, _require_perm


def setup_module():
    db.init_db()


def teardown_module():
    # 清理测试数据：撤销全部活跃 Token
    db.revoke_active_mcp_tokens()


# ── ContextVar 传播测试 ──────────────────────────────────

def test_contextvar_isolated_between_threads():
    """ContextVar 在不同线程中互不干扰"""
    results = {}

    def worker(name, record):
        _current_token_var.set(record)
        results[name] = _current_token_var.get()

    t1 = threading.Thread(target=worker, args=("t1", {"id": 1}))
    t2 = threading.Thread(target=worker, args=("t2", {"id": 2}))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results["t1"]["id"] == 1
    assert results["t2"]["id"] == 2


def test_require_perm_raises_when_no_token():
    """ContextVar 未设置时 _require_perm 必须抛出 PermissionError"""
    # 在新线程中运行，确保 ContextVar 为默认值（None）
    errors = []

    def worker():
        try:
            _require_perm("vault_list_credentials")
            errors.append("no_exception")
        except PermissionError as e:
            errors.append(str(e))

    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert len(errors) == 1
    assert "权限不足" in errors[0]


def test_require_perm_passes_with_valid_token():
    """ContextVar 设置了有效 token 记录后，已启用工具可正常通过"""
    errors = []

    def worker():
        # vault_list_credentials 默认全局启用
        token_record = {"id": 999, "tools_allowed": []}  # 空 = 继承全局
        _current_token_var.set(token_record)
        try:
            _require_perm("vault_list_credentials")
            errors.append("ok")
        except PermissionError as e:
            errors.append(str(e))

    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert errors == ["ok"], errors


# ── 双层权限逻辑测试 ─────────────────────────────────────

def test_global_switch_blocks_all_tokens():
    """全局关闭工具后，即使 Token 未限制范围也无法调用"""
    # 先确认 vault_get_secret 默认是关闭的（高危工具）
    assert not db.is_tool_enabled("vault_get_secret"), "vault_get_secret 应默认关闭"

    token_record = {"tools_allowed": []}  # 继承全局
    assert not _check_permission("vault_get_secret", token_record)


def test_global_switch_enabled_allows_inherit_token():
    """全局启用工具 + Token 继承全局 → 允许"""
    assert db.is_tool_enabled("vault_list_credentials")
    token_record = {"tools_allowed": []}
    assert _check_permission("vault_list_credentials", token_record)


def test_token_scope_restricts_to_subset():
    """Token 指定范围后，范围外的全局启用工具也被拒绝"""
    token_record = {"tools_allowed": ["vault_list_groups"]}
    assert _check_permission("vault_list_groups", token_record)
    assert not _check_permission("vault_list_credentials", token_record)


def test_token_scope_cannot_grant_globally_disabled():
    """Token 范围无法绕过全局关闭的工具"""
    token_record = {"tools_allowed": ["vault_get_secret"]}
    assert not _check_permission("vault_get_secret", token_record)


def test_all_tools_have_permission_entries():
    """数据库中应有全部 MCP 工具的权限记录（与 database._seed_mcp_permissions 的清单一致）"""
    perms = db.list_mcp_permissions()
    tool_names = {p["tool_name"] for p in perms}
    expected = {
        "vault_list_credentials", "vault_get_credential",
        "vault_create_credential", "vault_update_credential",
        "vault_delete_credential", "vault_verify_credential",
        "vault_batch_verify", "vault_list_groups", "vault_create_group",
        "vault_list_plugins", "vault_get_secret",
        # 备份导出/导入（默认关闭的高危工具）
        "vault_export_backup", "vault_import_backup",
    }
    assert expected == tool_names, f"缺失: {expected - tool_names}, 多余: {tool_names - expected}"


def test_default_enabled_tools():
    """默认启用的工具应恰好是 4 个只读工具"""
    perms = db.list_mcp_permissions()
    enabled = {p["tool_name"] for p in perms if p["enabled"]}
    assert enabled == {
        "vault_list_credentials", "vault_verify_credential",
        "vault_list_groups", "vault_list_plugins",
    }, f"默认启用集合不符: {enabled}"


# ── Token 生命周期测试 ───────────────────────────────────

def test_token_create_hash_lookup():
    """创建 Token → SHA-256 哈希存储 → 可查回"""
    plaintext = crypto.generate_token()
    token_hash = crypto.hash_token(plaintext)
    assert plaintext.startswith("cv_")

    record = db.create_mcp_token(name="test-perm", token_hash=token_hash,
                                 token_prefix=plaintext[:8], tools_allowed=[])
    found = db.get_token_by_hash(token_hash)
    assert found is not None
    assert found["name"] == "test-perm"
    assert found["token_prefix"] == plaintext[:8]
    assert found["id"] == record["id"]


def test_revoked_token_rejected():
    """撤销后 get_token_by_hash 应返回 None"""
    plaintext = crypto.generate_token()
    token_hash = crypto.hash_token(plaintext)
    db.create_mcp_token(name="test-revoke", token_hash=token_hash,
                        token_prefix=plaintext[:8], tools_allowed=[])
    assert db.get_token_by_hash(token_hash) is not None
    # 全量撤销（轮换用的同一入口；teardown 也走它）
    db.revoke_active_mcp_tokens()
    assert db.get_token_by_hash(token_hash) is None


def test_token_scope_honored_by_permission_check():
    """Token 自带工具范围时，权限检查按该范围放行/拒绝"""
    plaintext = crypto.generate_token()
    token_hash = crypto.hash_token(plaintext)
    db.create_mcp_token(name="test-scope", token_hash=token_hash,
                        token_prefix=plaintext[:8], tools_allowed=["vault_list_groups"])

    found = db.get_token_by_hash(token_hash)
    assert found["tools_allowed"] == ["vault_list_groups"]
    assert _check_permission("vault_list_groups", found)
    assert not _check_permission("vault_list_credentials", found)


# ── 种子同步规则（description 跟随源码默认，enabled 归用户） ──────

def test_seed_refreshes_description_but_keeps_enabled():
    """启动种子：已有工具的描述被源码默认值刷新，但用户的 enabled 开关不被覆盖。

    复现「老库描述追不上源码」的场景：把描述改成过期文案、开关反转，
    跑一次 db.init_db()（等价一次启动），应只见描述被刷回默认、enabled 原样。
    """
    tool = "vault_get_credential"
    orig = next(p for p in db.list_mcp_permissions() if p["tool_name"] == tool)
    orig_desc, orig_enabled = orig["description"], orig["enabled"]
    stale = "过期文案（掩码）"
    try:
        conn = db.get_conn()
        conn.execute(
            "UPDATE mcp_permissions SET description=?, enabled=? WHERE tool_name=?",
            (stale, 1 - int(orig_enabled), tool),
        )
        conn.commit()

        db.init_db()  # 复刻一次启动

        row = next(p for p in db.list_mcp_permissions() if p["tool_name"] == tool)
        assert row["description"] == orig_desc, f"描述未被刷新: {row['description']!r}"
        assert row["description"] != stale
        assert row["enabled"] == 1 - int(orig_enabled), "enabled 被种子覆盖（应保留用户开关）"
    finally:
        conn = db.get_conn()
        conn.execute(
            "UPDATE mcp_permissions SET description=?, enabled=? WHERE tool_name=?",
            (orig_desc, orig_enabled, tool),
        )
        conn.commit()


if __name__ == "__main__":
    setup_module()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    teardown_module()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
