"""凭据**数据格式**的历史升级（与「数据库结构迁移」是两件事）。

| | 结构迁移 | 数据升级（本模块） |
|---|---|---|
| 改什么 | 表结构（建表 / 加列 / 改约束） | 凭据**数据的格式**（某插件字段的拆并） |
| 脚本 | `scripts/migrations/m_*.py` | 本模块里的规则函数 |
| 何时跑 | 更新交棒阶段（应用已退出） | **应用解锁之后** |
| 为什么 | 只用 SQL，不需要主密码 | 凭据的**敏感字段是加密的**，交棒阶段拿不到主密码 |

**谁在哪跑**：
- 更新 / 换版后：解锁时比较 `settings.data_version` 与 `version.DATA_VERSION`，落后就把库里每条
  凭据过一遍规则（`api.run_data_upgrades`）。
- 导入备份：落库前用同一套规则把老格式转成当前格式（`backup.import_backup`）。

**规矩**：规则**只在这里维护**——插件应只描述**当前**格式、不写双格式兼容，框架其它地方也
不得为某个插件做格式容忍。规则必须**幂等**（重复跑无副作用）。
"""
from __future__ import annotations

from typing import Callable

# 规则：(credential_type 或 "*", 把老格式数据转成当前格式的函数)
# 函数收到**解密后的完整 data**，返回升级后的 data（无变化就原样返回）。
_Rule = Callable[[dict], dict]
_RULES: list[tuple[str, _Rule]] = []


def _rule(credential_type: str):
    """注册一条数据升级规则（模块加载时执行）。"""
    def deco(fn: _Rule) -> _Rule:
        _RULES.append((credential_type, fn))
        return fn
    return deco


@_rule("llm_wenxin")
def _wenxin_split_key(data: dict) -> dict:
    """文心 v1.1.0 起把凭证拆成 ``api_key`` + ``secret_key`` 两个字段
    （旧版只有一个 ``api_key``、要求拼成 ``API_KEY|SECRET_KEY``）。"""
    api = str(data.get("api_key") or "").strip()
    if "|" not in api or str(data.get("secret_key") or "").strip():
        return data                                  # 已是新格式（或本就没有 |），原样返回
    client_id, _, secret = api.partition("|")
    return {**data, "api_key": client_id.strip(), "secret_key": secret.strip()}


def upgrade_credential(credential_type: str, data: dict) -> dict:
    """把一条凭据的数据升级到**当前格式**（幂等）。没有适用规则时原样返回。"""
    out = data or {}
    for ct, rule in _RULES:
        if ct == "*" or ct == credential_type:
            out = rule(out)
    return out
