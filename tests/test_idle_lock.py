"""空闲自动锁定（backend/idle_lock.py）契约测试。

覆盖三块：
1. 纯函数 ``should_lock`` 的判定边界（关闭 / 未到点 / 到点 / 有任务顺延）；
2. 常量自洽（``OPTIONS`` 与 ``ALLOWED_MINUTES`` 必须一一对应，否则界面会给出后端拒绝的档位）；
3. 设置项 ``auto_lock_minutes`` 的读写往返与非法值拒绝。

注意：``tests/`` 直接跑**真实数据库**（无隔离，见 test_schedules.py 的风格），所以凡是
改动设置的地方都**先存原值、结束还原**。

运行：.venv/bin/python tests/test_idle_lock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import api
from backend import database as db
from backend import idle_lock

_ORIGINAL_SETTING = None


def setup_module():
    global _ORIGINAL_SETTING
    db.init_db()
    _ORIGINAL_SETTING = db.get_setting(idle_lock.SETTING_KEY, "")


def teardown_module():
    # 还原用户原本的设置，避免测试污染真库
    db.set_setting(idle_lock.SETTING_KEY, _ORIGINAL_SETTING)


# ── 1. 纯判定逻辑 ────────────────────────────────────────

def test_disabled_never_locks():
    """关闭（0 或负数）时永远不锁，哪怕空闲了非常久。"""
    assert idle_lock.should_lock(now=10_000.0, last=0.0, minutes=0, running_count=0) is False
    assert idle_lock.should_lock(now=10_000.0, last=0.0, minutes=-5, running_count=0) is False


def test_not_yet_idle_does_not_lock():
    """差 1 秒未到点：不锁。"""
    assert idle_lock.should_lock(now=59.0, last=0.0, minutes=1, running_count=0) is False


def test_locks_exactly_at_threshold():
    """正好到点即锁（边界取 `>=`，不是 `>`）。"""
    assert idle_lock.should_lock(now=60.0, last=0.0, minutes=1, running_count=0) is True
    assert idle_lock.should_lock(now=300.0, last=0.0, minutes=5, running_count=0) is True


def test_running_tasks_defer_the_lock():
    """到点了但后台有任务在跑：顺延（本轮不锁）；任务结束后仍空闲才锁。"""
    assert idle_lock.should_lock(now=3600.0, last=0.0, minutes=1, running_count=3) is False
    assert idle_lock.should_lock(now=3600.0, last=0.0, minutes=1, running_count=0) is True


# ── 2. 常量自洽 ──────────────────────────────────────────

def test_options_match_allowed_minutes():
    """界面下发的档位必须与后端白名单完全一致，顺序也一致。"""
    assert tuple(opt["value"] for opt in idle_lock.OPTIONS) == idle_lock.ALLOWED_MINUTES
    assert idle_lock.OPTIONS[0]["value"] == idle_lock.DEFAULT_MINUTES
    for opt in idle_lock.OPTIONS:
        assert isinstance(opt["label"], str) and opt["label"]


# ── 3. 设置项读写 ────────────────────────────────────────

def test_read_minutes_falls_back_on_garbage():
    """库里存了非法值 / 空值 → 回落到默认（关闭），不能让界面卡住。"""
    for bad in ("", "  ", "0x10", "abc", "7", "-1", "999"):
        db.set_setting(idle_lock.SETTING_KEY, bad)
        assert idle_lock.read_minutes() == idle_lock.DEFAULT_MINUTES, f"非法值 {bad!r} 未回落"


def test_get_auto_lock_shape():
    """get_auto_lock 必须同时给「当前值」与「可选档位」。"""
    db.set_setting(idle_lock.SETTING_KEY, "10")
    got = api.get_auto_lock()
    assert got["minutes"] == 10
    assert [opt["value"] for opt in got["options"]] == list(idle_lock.ALLOWED_MINUTES)


def test_set_auto_lock_round_trip():
    """写入合法档位后，读回来必须一致。"""
    for value in idle_lock.ALLOWED_MINUTES:
        res = api.set_auto_lock(value)
        assert res.get("ok") is True, f"档位 {value} 被拒: {res}"
        assert res["minutes"] == value
        assert idle_lock.read_minutes() == value
        assert db.get_setting(idle_lock.SETTING_KEY) == str(value)


def test_set_auto_lock_rejects_unknown_value():
    """不在白名单里的档位必须拒绝，且保持库中现值不变。"""
    db.set_setting(idle_lock.SETTING_KEY, "5")
    for bad in (2, 7, -1, 999):
        res = api.set_auto_lock(bad)
        assert res.get("ok") is False, f"非法档位 {bad} 竟被接受"
        assert db.get_setting(idle_lock.SETTING_KEY) == "5", "拒绝时不应改动库中值"


def test_set_auto_lock_rejects_non_numeric():
    """非数字输入（桥接传了字符串/None/列表）必须拒绝而不是抛异常。"""
    for bad in ("abc", None, [1], {}):
        res = api.set_auto_lock(bad)
        assert res.get("ok") is False, f"非数字 {bad!r} 竟被接受"


def test_set_auto_lock_rejects_float_string():
    """'5.5' 这类浮点串应被拒绝（int() 会抛，走的是非法分支）。"""
    res = api.set_auto_lock("5.5")
    assert res.get("ok") is False


def test_touch_resets_idle_clock():
    """touch() 之后空闲时长归零，不会立刻命中锁定。"""
    idle_lock.touch()
    last = idle_lock.last_activity()
    assert idle_lock.should_lock(now=last + 1.0, last=last, minutes=1, running_count=0) is False
    assert idle_lock.should_lock(now=last + 61.0, last=last, minutes=1, running_count=0) is True


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
