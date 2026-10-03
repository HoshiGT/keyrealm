"""
集成测试：定时计划（schedule）两个后端契约 bug 的修复验证
1. update_schedule 必须能以 (id, payload_dict) 位置参数形式调用（pywebview 桥接）
2. type-scoped 计划必须把 target_id 当作 credential_type 过滤，而非验证全部
运行：python tests/test_schedules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database as db
from backend import api
from backend import scheduler


def setup_module():
    db.init_db()


def teardown_module():
    for s in db.list_schedules():
        db.delete_schedule(s["id"])


# ── Bug 1: update_schedule 位置参数契约 ──────────────────

def test_update_schedule_accepts_payload_dict():
    """前端以 update_schedule(id, payload_dict) 调用，必须能解包更新"""
    sched = db.create_schedule(
        name="test-update", trigger_type="interval",
        trigger_args={"hours": 6}, target_scope="all", target_id=None,
    )
    # 模拟 pywebview 桥接：位置参数 (id, payload)
    result = api.update_schedule(sched["id"], {"name": "renamed", "enabled": False})
    assert isinstance(result, list), "update_schedule 应返回计划列表"

    updated = next(s for s in db.list_schedules() if s["id"] == sched["id"])
    assert updated["name"] == "renamed", f"name 未更新: {updated['name']}"
    assert not updated["enabled"], "enabled 未更新为 False"

    db.delete_schedule(sched["id"])


def test_update_schedule_signature_is_positional():
    """update_schedule 第二参数必须是普通位置参数（非 **kwargs），否则 JS 无法触达"""
    import inspect
    sig = inspect.signature(api.update_schedule)
    params = list(sig.parameters.values())
    # 不能有 VAR_KEYWORD（**kwargs）
    assert not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params), \
        "update_schedule 仍含 **kwargs，pywebview 桥接无法传入"
    # 第二个参数应为 payload（POSITIONAL_OR_KEYWORD）
    assert len(params) >= 2
    assert params[1].name == "payload", f"第二参数应为 payload，实际 {params[1].name}"


def test_update_schedule_empty_payload_noop():
    """空 payload 不应报错"""
    sched = db.create_schedule(
        name="test-noop", trigger_type="interval", trigger_args={"minutes": 30},
    )
    result = api.update_schedule(sched["id"], {})
    assert isinstance(result, list)
    db.delete_schedule(sched["id"])


# ── Bug 2: type-scoped 计划过滤 ──────────────────────────

def test_type_scoped_schedule_reads_target_id_as_credential_type():
    """_run_schedule 在 scope=='type' 时应把 target_id 作为 credential_type 传给 batch_verify"""
    captured = {}

    def fake_batch_verify(scope="all", group_id=None, credential_type=None, trigger_type="manual"):
        captured["scope"] = scope
        captured["group_id"] = group_id
        captured["credential_type"] = credential_type

    orig = scheduler.batch_verify
    orig_update = db.update_schedule
    scheduler.batch_verify = fake_batch_verify
    db.update_schedule = lambda *a, **k: None  # 避免写库
    try:
        schedule = {
            "id": 999,
            "target_scope": "type",
            "target_id": "llm_api_key",  # 前端把类型名存进 target_id
        }
        scheduler._run_schedule(schedule)
    finally:
        scheduler.batch_verify = orig
        db.update_schedule = orig_update

    assert captured["scope"] == "type"
    assert captured["credential_type"] == "llm_api_key", \
        f"type-scoped 计划未正确读取 credential_type: {captured}"
    assert captured["group_id"] is None


def test_group_scoped_schedule_reads_target_id_as_group_id():
    """_run_schedule 在 scope=='group' 时应把 target_id 作为 group_id"""
    captured = {}

    def fake_batch_verify(scope="all", group_id=None, credential_type=None, trigger_type="manual"):
        captured.update(scope=scope, group_id=group_id, credential_type=credential_type)

    orig = scheduler.batch_verify
    orig_update = db.update_schedule
    scheduler.batch_verify = fake_batch_verify
    db.update_schedule = lambda *a, **k: None
    try:
        scheduler._run_schedule({"id": 998, "target_scope": "group", "target_id": 5})
    finally:
        scheduler.batch_verify = orig
        db.update_schedule = orig_update

    assert captured["scope"] == "group"
    assert captured["group_id"] == 5
    assert captured["credential_type"] is None


def test_target_id_roundtrips_int_and_str():
    """target_id 列须能同时保存 int(分组) 与 str(类型) 并原样读回（SQLite 动态类型亲和）"""
    g = db.create_schedule(name="grp", trigger_type="interval", trigger_args={"hours": 1},
                           target_scope="group", target_id=7)
    t = db.create_schedule(name="typ", trigger_type="interval", trigger_args={"hours": 1},
                           target_scope="type", target_id="llm_api_key")

    g_read = next(s for s in db.list_schedules() if s["id"] == g["id"])
    t_read = next(s for s in db.list_schedules() if s["id"] == t["id"])

    assert g_read["target_id"] == 7 and isinstance(g_read["target_id"], int), \
        f"分组 target_id 应为 int 7，实际 {g_read['target_id']!r}"
    assert t_read["target_id"] == "llm_api_key" and isinstance(t_read["target_id"], str), \
        f"类型 target_id 应为 str，实际 {t_read['target_id']!r}"

    db.delete_schedule(g["id"])
    db.delete_schedule(t["id"])


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
