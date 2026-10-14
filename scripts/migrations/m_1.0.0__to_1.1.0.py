"""1.0.0 → 1.1.0：**表结构无变化**。

本跳对应「百度文心凭证由单字段 ``API_KEY|SECRET_KEY`` 拆成 ``api_key`` + ``secret_key``」——
属于**数据结构（数据格式）变化**。按规矩，这类变化同样提升 ``SCHEMA_VERSION``、同样留一跳脚本，
好让更新流程把 ``settings.schema_version`` 推进到 1.1.0（迁移链完整，不缺跳）。

**数据部分不在这里做**：凭据的敏感字段是**加密**的，而本脚本跑在更新交棒阶段（应用已退出、
没有主密码），读不出也改不了那些值。实际转换由 ``backend/data_upgrades.py`` 的
``_wenxin_split_key`` 规则负责，在**应用解锁后**执行一次（``api.run_data_upgrades``）；
导入老备份时也在落库前走同一套规则。
"""

FROM_VERSION = "1.0.0"
TO_VERSION = "1.1.0"


def preview(conn) -> dict:
    """只读盘点：本跳不动表结构。"""
    return {"note": "无表结构变化（仅凭据数据格式；由解锁后的数据升级完成）"}


def apply(conn) -> None:
    """本跳无表结构改动——运行器只据此把 ``schema_version`` 推进到 1.1.0。"""
    return None
