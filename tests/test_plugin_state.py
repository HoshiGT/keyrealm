"""
插件启用/禁用状态：plugins/.state.json 的行为契约

覆盖用户拍板的机制：只有 plugins/ 一份插件源，启用/禁用写在 plugins/.state.json；
文件缺失或损坏一律退化为「全部启用」；禁用插件的功能闸门（get_module）关闭。

**全程沙盒**：DB 与状态文件都落到 ./tmp，绝不写真实的 data/credentials.db
与 plugins/.state.json；teardown 还会断言真实状态文件字节未变。

运行：python tests/test_plugin_state.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SANDBOX = ROOT / "tmp" / "test_plugin_state"

from backend import database as db

# 沙盒 DB（必须在任何连接建立之前改）
db.DATA_DIR = SANDBOX
db.DB_PATH = SANDBOX / "probe.db"

from backend import plugin_manager as pm

_REAL_STATE_FILE = pm.STATE_FILE
_realsnapshot = None

CT = "im_dingtalk"       # 真实存在的插件（沙盒里由 sync_plugins 从 plugins/ 读到）
OTHER = "im_wecom"
GHOST = "no_such_credential_type"


def _write_state(payload: str | None):
    """直接写状态文件并强制改 mtime，模拟「用户手工编辑」。"""
    if payload is None:
        pm.STATE_FILE.unlink(missing_ok=True)
        return
    pm.STATE_FILE.write_text(payload, encoding="utf-8")
    now = os.stat(pm.STATE_FILE).st_mtime + 1
    os.utime(pm.STATE_FILE, (now, now))


def setup_module():
    global _realsnapshot
    # 记下真实状态文件原貌（它通常根本不存在）
    _realsnapshot = _REAL_STATE_FILE.read_bytes() if _REAL_STATE_FILE.exists() else None

    SANDBOX.mkdir(parents=True, exist_ok=True)
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
    # 状态文件重定向到沙盒；PLUGINS_DIR 保持真实（只读扫描）
    pm.STATE_FILE = SANDBOX / ".state.json"
    pm.STATE_FILE.unlink(missing_ok=True)

    db.init_db()
    pm.sync_plugins()          # 把真实 plugins/ 的记录同步进沙盒 DB（只读磁盘）


def teardown_module():
    # 归还真实路径，并断言真实状态文件没被这次测试碰过
    pm.STATE_FILE = _REAL_STATE_FILE
    now = _REAL_STATE_FILE.read_bytes() if _REAL_STATE_FILE.exists() else None
    assert now == _realsnapshot, "真实 plugins/.state.json 被测试污染了！"
    if SANDBOX.exists():
        for f in SANDBOX.iterdir():
            f.unlink()
        try:
            SANDBOX.rmdir()          # 沙盒目录也一并收走，别在 tmp/ 里留空壳
        except OSError:
            pass


# ── 状态文件解析 ──────────────────────────────────────────

def test_missing_state_file_means_all_enabled():
    """文件不存在 = 出厂默认，全部启用。"""
    _write_state(None)
    assert pm._read_disabled() == set()
    assert pm.is_plugin_enabled(CT) is True
    assert pm.is_plugin_enabled(OTHER) is True


def test_disabled_entry_blocks_only_that_type():
    _write_state(json.dumps({"disabled": [CT]}))
    assert pm.is_plugin_enabled(CT) is False
    assert pm.is_plugin_enabled(OTHER) is True


def test_corrupt_state_file_falls_back_to_all_enabled():
    """坏 JSON / 非 dict / disabled 非 list —— 一律全启用，绝不能让插件集体消失。"""
    for bad in ("{{{ not json", '["a"]', json.dumps({"disabled": "im_dingtalk"}),
                json.dumps({"disabled": 123}), ""):
        _write_state(bad)
        assert pm._read_disabled() == set(), f"坏内容 {bad!r} 应退化为全启用"
        assert pm.is_plugin_enabled(CT) is True


def test_unknown_keys_are_ignored():
    """状态文件里混入非字符串项不该炸，也不该把插件关掉。"""
    _write_state(json.dumps({"disabled": [CT, 123, None, {"x": 1}]}))
    assert pm._read_disabled() == {CT}
    assert pm.is_plugin_enabled(OTHER) is True


# ── 功能闸门 ──────────────────────────────────────────────

def test_disabled_plugin_gate_closes():
    """禁用 = get_module 返回 None，且所有能力自然降级。"""
    _write_state(None)
    pm.sync_plugins()
    assert pm.get_module(CT) is not None, "前提：sync 后该插件应已加载"

    result = pm.set_plugin_enabled(CT, False)
    assert result["ok"] is True
    assert pm.get_module(CT) is None
    assert pm.has_actions(CT) is False
    assert pm.plugin_actions(CT, {}) == []
    assert pm.plugin_models(CT, {}) == []
    assert pm.plugin_capabilities(CT, {})["error"] is True
    assert pm.get_display_summary(CT, {}) == ""
    assert pm.plugin_uses_proxy(CT) is False
    assert pm.verify_credential(CT, {}).status == "error"
    assert pm.run_plugin_action(CT, "x", {}, {})["ok"] is False


def test_disabled_message_says_disabled_not_missing():
    """提示必须是「插件已禁用」，不能误导用户去翻插件目录。"""
    pm.set_plugin_enabled(CT, False)
    msg = pm._module_missing_error(CT)
    assert "插件已禁用" in msg
    assert "未安装" not in msg
    assert pm.validate_credential(CT, {}) == [f"插件已禁用: {CT}"]


def test_reenable_restores_module():
    pm.set_plugin_enabled(CT, False)
    assert pm.get_module(CT) is None
    assert pm.set_plugin_enabled(CT, True)["ok"] is True
    assert pm.get_module(CT) is not None
    assert pm._read_disabled() == set()


def test_set_enabled_unknown_plugin_rejected():
    _write_state(None)
    result = pm.set_plugin_enabled(GHOST, False)
    assert result["ok"] is False
    assert pm.is_plugin_enabled(GHOST) is True, "不该往状态文件里写入不存在的插件"


# ── 镜像与清理 ────────────────────────────────────────────

def test_db_enabled_column_mirrors_state_file():
    """DB 的 enabled 列是只读镜像：只由 sync_plugins 写，取值跟随 .state.json。"""
    pm.set_plugin_enabled(CT, False)
    assert bool(db.get_plugin_by_type(CT).get("enabled")) is False
    assert bool(db.get_plugin_by_type(OTHER).get("enabled")) is True

    pm.set_plugin_enabled(CT, True)
    assert bool(db.get_plugin_by_type(CT).get("enabled")) is True


def test_sync_cleans_stale_keys():
    """插件目录已不存在时，状态文件里的残留 key 要被清掉，避免无限膨胀。"""
    _write_state(json.dumps({"disabled": [GHOST, CT]}))
    pm.sync_plugins()
    assert json.loads(pm.STATE_FILE.read_text(encoding="utf-8"))["disabled"] == [CT]


def test_disabled_metadata_still_resolvable():
    """禁用只关功能闸门，不抹掉元数据（否则备份导入/预设/密钥净化会静默劣化）。"""
    pm.set_plugin_enabled(CT, False)
    record = db.get_plugin_by_type(CT)
    assert record is not None
    assert record["fields_schema"], "禁用插件的字段 schema 仍须可取到"


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
