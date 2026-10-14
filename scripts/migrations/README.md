# 数据库逐级迁移

本目录放**数据库结构**的逐级迁移脚本。与应用版本无关——这里的版本是
`settings.schema_version`（源码里的 `backend/version.py::SCHEMA_VERSION`）。

## 铁律：只提供「低一级 → 高一级」

**绝不写跨级脚本**（如 `1.0.0 → 1.2.0`）。升级时由运行器
（`scripts/db_update.py` / `backend/updater.run_migrations`）把
`1.0.0 → 1.1.0`、`1.1.0 → 1.2.0` 这样一层层串起来执行。理由：跨级脚本随版本增多呈组合爆炸，
后期无法维护；逐级脚本每份只关心相邻两版，永远只需写一份、也只可能错一处。

## 命名与接口

文件名：`m_<from>__to_<to>.py`，例如：

```
m_1.0.0__to_1.1.0.py
m_1.1.0__to_1.2.0.py
```

每个脚本必须暴露：

```python
FROM_VERSION = "1.0.0"     # 本脚本负责的「起始」结构版本
TO_VERSION   = "1.1.0"     # 执行完后的「目标」结构版本

def preview(conn) -> dict:
    """只读盘点，供演练打印（返回可 JSON 化的 dict）。"""
    ...

def apply(conn) -> None:
    """幂等写入。运行器会在 `with conn:` 事务里调用它，
    **成功后**才把 settings.schema_version 写成 TO_VERSION；
    抛异常则整跳回滚。"""
    ...
```

- `conn` 是 `sqlite3.Connection`（`row_factory=sqlite3.Row`）。
- 只做这一步的改动；不要在这里再改 `schema_version`（运行器负责）。
- **幂等**：同一跳被执行两次也不能出错（运行器正常只跑一次，但幂等是底线）。
- 模块顶层**无副作用**（运行器会 import 它来读 `FROM_VERSION`/`TO_VERSION`）。

## 运行

```bash
python scripts/db_update.py            # 演练：打印将执行的链，不写库
python scripts/db_update.py --apply    # 真正写库（每跳前留 SQLite 快照；先抢单实例锁）
```

`--apply` 时会先 `single_instance.acquire()`，**应用在跑就拒绝执行**。
旧库（`schema_version` 为 1.0 之前的整数）不在本机制管辖范围，运行器会明确拒绝、不代劳。

## 与「数据升级」的分工

本目录只管**表结构**。**凭据数据的格式变化**（某插件字段的拆并这类）走另一条路：
**`backend/data_upgrades.py`** + `version.DATA_VERSION`。原因是**时机**——凭据的敏感字段是
**加密**的，而这里的迁移跑在更新交棒阶段（应用已退出、没有主密码），**改不了那些数据**。
所以数据升级在**应用解锁后**执行一次（幂等），导入备份时也在落库前走同一套规则。详见该模块。
