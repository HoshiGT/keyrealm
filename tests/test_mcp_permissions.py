"""
集成测试：MCP 双层权限（全局开关 ∩ Token 范围）+ ContextVar 传播

**全程沙盒**：DB 落到 ./tmp，绝不碰真实的 data/credentials.db。
（本套件里有「全量撤销活跃 Token」这类清理动作，跑在真库上会把用户自己的
MCP Token 一并作废——这也是把它沙盒化的原因。）

运行：python -m pytest tests/test_mcp_permissions.py -v
或直接：python tests/test_mcp_permissions.py
"""
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = ROOT / "tmp" / "test_mcp_permissions"

from backend import database as db

# 沙盒 DB（必须在任何连接建立之前改）
db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "probe.db"

from backend import crypto
from backend.mcp_server import _current_token_var, _perm_denied


def setup_module():
    SANDBOX.mkdir(parents=True, exist_ok=True)
    db.init_db()


def teardown_module():
    # 清理沙盒数据；沙盒目录也一并收走，别在 tmp/ 里留空壳
    db.revoke_active_mcp_tokens()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)


def _in_thread(fn):
    """在**新线程**里跑 fn：ContextVar 在新线程里必为默认值（None），

    免得前一个用例在主线程设下的 token 记录串到下一个用例里。
    """
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


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


# ── 权限检查（返回结构化拒绝，不抛异常） ──────────────────

def test_no_identity_is_denied():
    """ContextVar 未设置（认不出调用身份）时必须拒绝，并给出可读原因"""
    denied = _in_thread(lambda: _perm_denied("vault_list_credentials"))
    assert isinstance(denied, dict), denied
    assert denied["error"] == "permission_denied"
    assert denied["reason"] == "no_identity"
    # 原因要能原样送到用户眼前：点名是哪个工具
    assert "vault_list_credentials" in denied["message"]


def test_enabled_tool_passes_with_inherit_token():
    """全局启用 + Token 继承全局（tools_allowed 为空）→ 放行（返回 None）"""
    assert db.is_tool_enabled("vault_list_credentials")

    def probe():
        _current_token_var.set({"id": 999, "tools_allowed": []})
        return _perm_denied("vault_list_credentials")

    assert _in_thread(probe) is None


def test_global_switch_blocks_even_with_token_scope():
    """全局关闭的高危工具，Token 范围也授不了权 → 拒绝，原因 global_disabled"""
    assert not db.is_tool_enabled("vault_get_secret"), "vault_get_secret 应默认关闭"

    def probe():
        _current_token_var.set({"id": 1, "tools_allowed": ["vault_get_secret"]})
        return _perm_denied("vault_get_secret")

    denied = _in_thread(probe)
    assert denied and denied["reason"] == "global_disabled", denied


def test_token_scope_restricts_to_subset():
    """Token 指定范围后，范围外的全局启用工具也被拒绝（原因 token_scope）"""

    def probe_allowed():
        _current_token_var.set({"id": 1, "tools_allowed": ["vault_list_groups"]})
        return _perm_denied("vault_list_groups")

    def probe_denied():
        _current_token_var.set({"id": 1, "tools_allowed": ["vault_list_groups"]})
        return _perm_denied("vault_list_credentials")

    assert _in_thread(probe_allowed) is None
    denied = _in_thread(probe_denied)
    assert denied and denied["reason"] == "token_scope", denied


# ── 权限种子清单 ─────────────────────────────────────────

def test_all_tools_have_permission_entries():
    """数据库中应有全部 MCP 工具的权限记录（与 database._seed_mcp_permissions 的清单一致）"""
    perms = db.list_mcp_permissions()
    tool_names = {p["tool_name"] for p in perms}
    expected = {
        "vault_list_credentials", "vault_get_credential",
        "vault_create_credential", "vault_update_credential",
        "vault_delete_credential", "vault_verify_credential",
        "vault_batch_verify", "vault_list_groups", "vault_create_group",
        "vault_delete_group",
        "vault_list_plugins", "vault_get_secret",
        # 备份导出/导入（默认关闭的高危工具）
        "vault_export_backup", "vault_import_backup",
        # 验证代理管理
        "vault_list_proxies", "vault_create_proxy", "vault_update_proxy",
        "vault_delete_proxy", "vault_set_global_proxy", "vault_test_proxy",
    }
    assert expected == tool_names, f"缺失: {expected - tool_names}, 多余: {tool_names - expected}"


def test_default_enabled_tools():
    """默认启用的工具应恰好是 5 个只读工具"""
    perms = db.list_mcp_permissions()
    enabled = {p["tool_name"] for p in perms if p["enabled"]}
    assert enabled == {
        "vault_list_credentials", "vault_verify_credential",
        "vault_list_groups", "vault_list_plugins", "vault_list_proxies",
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

    def allowed():
        _current_token_var.set(found)
        return _perm_denied("vault_list_groups")

    def denied():
        _current_token_var.set(found)
        return _perm_denied("vault_list_credentials")

    assert _in_thread(allowed) is None
    assert _in_thread(denied) and _in_thread(denied)["reason"] == "token_scope"


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
