"""输出净化：把插件结果 / 日志里回显的**敏感字段值**替换成 ``***``。

为什么必须有这一层：插件在错误提示、``account_info``、列表类操作的 ``rows`` 里**回显
上游原始响应**是常态（body_snippet 把上游报错原样带进 message；部分服务的密钥就在 URL
query 里，异常文本天然带着它）。这些文本有两条**持久化**去向：

- 落盘到 ``data/logs/credential_vault.log``（见 backend/scheduler.py 的验证结果日志）
- 落库到凭据的 ``status_detail`` 列

所以净化必须在**框架侧统一做**，不能指望每个插件自己记得。本模块被两处共用：

- ``plugin_manager.verify_credential`` —— 单条 / 批量 / MCP / 定时四条 verify 路径的唯一咽喉
- ``api.run_credential_action`` / ``api.stream_credential_action`` —— 操作结果

设计成**纯函数**（显式接收敏感值列表，不自己查库），避免 ``plugin_manager`` 与
``api`` 互相 import 形成环。
"""
from __future__ import annotations

from typing import Any

from . import logger

_log = logger.get_logger(__name__)

# 短于这个长度的敏感值不参与替换。单字符/两三字符的串会出现在几乎任何文本里
# （密钥 "1" 遇上 "HTTP/1.1"），替换只会把给用户看的消息打成筛子、把日志搞成天书。
# 真实密钥远长于此，所以这个阈值挡掉的基本都是「本来就不该被当成密钥」的短值。
MIN_SECRET_LEN = 4

# 已经告警过的 (credential_type, field_key)：同一个字段不重复刷日志
_warned_short: set[tuple[str, str]] = set()


def secrets_of(fields_schema: Any, data: dict, credential_type: str = "") -> list[str]:
    """从插件字段声明里取出**所有敏感字段的明文值**。

    ``fields_schema`` 是 ``plugins.fields_schema``（即 manifest 的 ``fields`` 数组），
    只有 ``sensitive: true`` 的字段算密钥。

    两种「静默不保护」在这里转成可见的 WARNING：

    - **字段值短于阈值**——即便传下去也不会被替换，等于没保护；
    - 由 ``plugin_manager.sync_plugins`` 负责告警的那类「manifest 里一个敏感字段都没有」。

    """
    values: list[str] = []
    for field in fields_schema or []:
        if not isinstance(field, dict) or not field.get("sensitive"):
            continue
        key = str(field.get("key") or "")
        raw = (data or {}).get(key)
        if raw is None or raw == "":
            continue
        text = str(raw)
        if len(text) < MIN_SECRET_LEN:
            marker = (credential_type, key)
            if marker not in _warned_short:
                _warned_short.add(marker)
                _log.warning(
                    "凭据字段 %s.%s 的值只有 %d 字符（短于净化阈值 %d），不会被替换；"
                    "若它确是密钥，请改用一个更长的字段承载",
                    credential_type or "?", key, len(text), MIN_SECRET_LEN,
                )
            continue
        values.append(text)
    return values


def redact_text(text: Any, secrets: Any) -> Any:
    """把文本里出现的敏感值替换成 ``***``。

    只处理字符串；短于 ``MIN_SECRET_LEN`` 的值跳过（见该常量的说明）。
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for secret in secrets or []:
        if secret and len(secret) >= MIN_SECRET_LEN:
            out = out.replace(secret, "***")
    return out


def redact_deep(value: Any, secrets: Any) -> Any:
    """递归净化整个结果。

    **不能只扫顶层的 ``output`` / ``error``**：列表类操作把数据放在 ``rows`` / ``columns`` 里，
    只扫顶层等于让列表里回显的密钥原样穿到前端（JS 桥那一侧没有任何净化）。
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: redact_deep(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_deep(v, secrets) for v in value]
    return value
