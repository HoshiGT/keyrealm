"""1.2.0 → 1.3.0：清掉两条**再也没人读**的 settings 死键。

- ``data_version``：早期「数据版本号」（`version.DATA_VERSION`）方案的残留——后来改用
  `SCHEMA_VERSION`、又改成幂等，这个键再没人读写。
- ``data_schema_version``：上一轮的「数据升级进度」标记——已改为**幂等**（不记进度）。

两者在代码里都是 **0 引用**（`grep -rn` 可验）。纯 SQL、不需要主密码，故可在交棒阶段完成。
"""

FROM_VERSION = "1.2.0"
TO_VERSION = "1.3.0"

_DEAD_KEYS = ("data_version", "data_schema_version")


def preview(conn) -> dict:
    """只读盘点：还有几条死键没清。"""
    n = 0
    for k in _DEAD_KEYS:
        n += conn.execute("SELECT COUNT(*) AS c FROM settings WHERE key=?", (k,)).fetchone()["c"]
    return {"dead_settings_keys": n, "keys": list(_DEAD_KEYS)}


def apply(conn) -> None:
    conn.execute(
        "DELETE FROM settings WHERE key IN (%s)" % ",".join("?" * len(_DEAD_KEYS)),
        _DEAD_KEYS,
    )
