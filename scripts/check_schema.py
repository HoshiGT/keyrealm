"""在临时库上真跑一遍建表与各表 CRUD —— **打包/发版前必跑**。

为什么需要它
------------
建表语句写在 `backend/database.py` 的 `executescript` 里，只有在**真的连上 SQLite**
时才会被解析。而绝大多数单元测试会把 `database` 模块整个打桩（因为要避免动真库），
于是「SQL 本身写错了」这类问题**一通测试全绿、一打包运行就崩**。

真实案例：`preset_defaults` 表曾把列名取成 `values` —— 那是 SQLite 的**保留字**，
`CREATE TABLE ... values TEXT` 直接 `syntax error`。同批还有一处 `SELECT values FROM ...`
在改名后漏改。两次都是靠这个脚本逮住的，而不是靠打桩的测试。

所以：**改了表结构、加了新表、或准备打包时，先跑它**。
它只碰临时目录，不读写 `data/` 里的真库。

用法
----
    python scripts/check_schema.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 输出里用了 ✓ / ✗ —— 这些符号**不在 GBK 里**，中文 Windows 控制台直接跑会抛
# UnicodeEncodeError。把标准输出钉成 UTF-8（build.py 也是按 UTF-8 读它的）。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend import database as db  # noqa: E402  (必须在 sys.path 调整之后导入)

# 建表脚本里应当出现的每一张表
EXPECTED_TABLES = (
    "settings", "groups", "credentials", "plugins", "mcp_permissions",
    "mcp_tokens", "schedules", "proxies", "preset_defaults",
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vault-schema-check-"))
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "credentials.db"
    try:
        # 1) 建表 —— SQL 写错会在这里原样抛出，正是我们要挡住的
        db.init_db()
        conn = db.get_conn()
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in EXPECTED_TABLES if name not in tables]
        if missing:
            print(f"✗ 缺表：{missing}", file=sys.stderr)
            return 1
        print(f"✓ 建表通过（{len(tables)} 张表）")

        # 2) 设置表
        db.set_setting("verify_proxy", "socks5://x:1")
        assert db.get_setting("verify_proxy") == "socks5://x:1"

        # 3) 代理表 CRUD
        first = db.create_proxy("生产梯子", "socks5://1.2.3.4:1080")
        second = db.create_proxy("备用", "http://127.0.0.1:7890")
        assert {p["id"] for p in db.list_proxies()} == {first["id"], second["id"]}
        assert db.get_proxy(first["id"])["name"] == "生产梯子"
        assert db.update_proxy(first["id"], name="改过的")["name"] == "改过的"
        assert db.delete_proxy(second["id"]) is True
        print("✓ 代理表 CRUD 通过")

        # 4) 默认设置表 CRUD（列名是 payload，不是保留字 values）
        db.set_preset_defaults("category", "llm", {"base_url": "https://cat/v1"})
        db.set_preset_defaults("plugin", "llm_glm", {"base_url": "https://glm/v1"})
        assert db.get_preset_defaults("plugin", "llm_glm") == {"base_url": "https://glm/v1"}
        assert db.get_preset_defaults("plugin", "不存在") == {}
        rows = db.list_preset_defaults()
        assert all("payload" in row for row in rows), "list_preset_defaults 应返回 payload 列"
        db.delete_preset_defaults("plugin", "llm_glm")
        assert db.get_preset_defaults("plugin", "llm_glm") == {}
        print("✓ 默认设置表 CRUD 通过")

        # 5) 插件表 upsert（每次启动都会走）
        db.upsert_plugin(name="Demo", version="1.0.0", credential_type="llm_demo",
                         fields_schema=[{"key": "api_key", "sensitive": True}],
                         plugin_path=str(tmp), author="", description="", icon="🔑",
                         category="llm", category_name="LLM 模型", batch_verify=True)
        db.upsert_plugin(name="Demo2", version="2.0.0", credential_type="llm_demo",
                         fields_schema=[], plugin_path=str(tmp), author="", description="",
                         icon="🔑", category="dns", category_name="域名解析", batch_verify=False)
        record = db.get_plugin_by_type("llm_demo")
        assert record["category"] == "dns" and record["batch_verify"] == 0, record
        print("✓ 插件表 upsert 通过")

        # 6) 分组 / 凭据
        group = db.create_group("测试组", "", "#fff")
        cred = db.create_credential(name="c1", credential_type="llm_demo",
                                    data_encrypted="BLOB", data_plain={"notes": "n"},
                                    group_id=group["id"])
        assert db.get_credential(cred["id"])["group_id"] == group["id"]
        print("✓ 分组 / 凭据通过")

        # 7) 幂等：打包后每次启动都会跑 init_db
        db.init_db()
        db.init_db()
        print("✓ 重复 init_db 幂等")

        # 8) 升级路径：把这两次新加的表删掉，再跑一次应当补建（不需要迁移、不需要改版本号）
        conn.execute("DROP TABLE preset_defaults")
        conn.execute("DROP TABLE proxies")
        conn.commit()
        db.init_db()
        rebuilt = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"proxies", "preset_defaults"} <= rebuilt, rebuilt
        print("✓ 老库补建新表（升级路径）")

        # 9) 代理 id 由「自增整数」改成「UUID」的升级路径：老表必须被重建，
        #    否则 uuid 文本塞不进 INTEGER PRIMARY KEY（rowid 别名）→ 建代理直接报类型错。
        conn.execute("DROP TABLE proxies")
        conn.execute(
            "CREATE TABLE proxies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " url TEXT NOT NULL, sort_order INTEGER DEFAULT 0,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO proxies (name, url) VALUES ('老代理','socks5://127.0.0.1:1080')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('global_proxy_id','1')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version','4')")
        conn.commit()
        db.init_db()
        cols = {r["name"]: str(r["type"] or "").upper()
                for r in conn.execute("PRAGMA table_info(proxies)")}
        assert cols.get("id") == "TEXT", cols
        assert db.list_proxies() == [], "旧的自增 id 行应被丢弃（1.0 前不做数据迁移）"
        assert db.get_setting("global_proxy_id", "") == "", "旧引用必须清掉，否则指向不存在的代理"
        created = db.create_proxy("新代理", "socks5://127.0.0.1:10808")
        assert isinstance(created["id"], str) and len(created["id"]) == 32, created
        print("✓ 代理 id 改 UUID（老库重建 + 引用清理）")

        print("\n全部通过：建表脚本与各表 SQL 都可用。")
        return 0
    finally:
        try:
            db._conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
