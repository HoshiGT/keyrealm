"""
集成测试：凭据编辑不覆盖密钥（数据丢失修复验证）
1. get_credential 解锁后返回完整明文 data（含敏感字段），供编辑表单预填
2. update_credential 在敏感字段为空/缺失时保留已存明文，避免覆盖密钥
运行：python tests/test_credential_update.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database as db
from backend import api, crypto, plugin_manager


def setup_module():
    db.init_db()
    plugin_manager.bootstrap_plugins()
    # 直接注入测试密钥，模拟已解锁状态
    salt = crypto.generate_salt()
    api._set_key(crypto.derive_key("test-password", salt))


def teardown_module():
    for c in db.list_credentials():
        db.delete_credential(c["id"])
    api._set_key(None)


def _create_cred():
    return api.create_credential(
        name="test-secret", credential_type="llm_openai",
        data={"base_url": "https://api.openai.com/v1",
              "api_key": "sk-real-secret", "notes": "hello"},
        auto_verify=False,
    )


def test_get_credential_returns_full_decrypted_data():
    """get_credential 解锁后应返回含敏感字段的完整明文 data"""
    cred = _create_cred()
    full = api.get_credential(cred["id"])

    assert "data" in full, "get_credential 未返回 data 字段"
    assert full["data"]["api_key"] == "sk-real-secret", \
        f"敏感字段未解密返回: {full['data'].get('api_key')!r}"
    # data_plain 仍应保持非敏感（不含 api_key），供列表展示
    assert "api_key" not in (full.get("data_plain") or {}), \
        "data_plain 不应包含敏感字段"


def test_update_credential_preserves_empty_sensitive_field():
    """敏感字段提交空值时，应保留已存密钥而非覆盖为空"""
    cred = _create_cred()

    # 模拟编辑表单未预填敏感字段 → 提交空 api_key
    api.update_credential(cred["id"], data={
        "base_url": "https://api.openai.com/v1/chat",  # 改了非敏感字段
        "api_key": "",           # 空 → 应保留原值
        "notes": "updated",
    })

    full = api.get_credential(cred["id"])
    assert full["data"]["api_key"] == "sk-real-secret", \
        f"密钥被覆盖为空或丢失: {full['data'].get('api_key')!r}"
    assert full["data"]["base_url"] == "https://api.openai.com/v1/chat", "非敏感字段应正常更新"


def test_update_credential_preserves_missing_sensitive_field():
    """敏感字段完全缺失时，也应保留已存密钥"""
    cred = _create_cred()

    api.update_credential(cred["id"], data={
        "base_url": "https://api.openai.com/v1",
        "notes": "partial",
    })

    full = api.get_credential(cred["id"])
    assert full["data"]["api_key"] == "sk-real-secret", \
        f"缺失的敏感字段应保留: {full['data'].get('api_key')!r}"


def test_update_credential_allows_changing_secret():
    """用户主动输入新密钥时，应正常更新（不被防御逻辑拦截）"""
    cred = _create_cred()

    api.update_credential(cred["id"], data={
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-new-secret",
        "notes": "",
    })

    full = api.get_credential(cred["id"])
    assert full["data"]["api_key"] == "sk-new-secret", \
        f"新密钥未生效: {full['data'].get('api_key')!r}"


def test_update_credential_group_id_none_ungroups():
    """显式传 group_id=None 应取消分组（而非被当作“不修改”）"""
    group = db.create_group("temp-group", "")
    cred = api.create_credential(
        name="grouped-secret", credential_type="llm_openai",
        data={"base_url": "https://api.openai.com/v1", "api_key": "sk-g", "notes": ""},
        group_id=group["id"], auto_verify=False,
    )
    assert db.get_credential(cred["id"])["group_id"] == group["id"]

    # 模拟前端清空分组：group_id 传 None
    api.update_credential(cred["id"], group_id=None)

    assert db.get_credential(cred["id"])["group_id"] is None, \
        "group_id=None 未取消分组"

    db.delete_credential(cred["id"])
    db.delete_group(group["id"])


def test_update_credential_group_id_omitted_keeps_group():
    """省略 group_id（哨兵默认值）应保持原分组不变"""
    group = db.create_group("temp-group-2", "")
    cred = api.create_credential(
        name="keep-group", credential_type="llm_openai",
        data={"base_url": "https://api.openai.com/v1", "api_key": "sk-k", "notes": ""},
        group_id=group["id"], auto_verify=False,
    )
    # 只改 name，不传 group_id
    api.update_credential(cred["id"], name="renamed")
    assert db.get_credential(cred["id"])["group_id"] == group["id"], \
        "省略 group_id 时不应修改分组"

    db.delete_credential(cred["id"])
    db.delete_group(group["id"])


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
