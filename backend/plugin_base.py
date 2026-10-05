"""
插件基类与接口定义
所有插件必须实现 verify(data: dict) -> VerifyResult
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .proxy import SOCKS_HINT

_log = logging.getLogger("plugin")


def _friendly_error(e: Exception) -> str:
    """插件异常 → 展示给用户的文本。

    只做一处替换：httpx 在缺 socksio 时抛英文 ImportError（"…'socksio' package is not
    installed…"），这句话对用户没有可操作性，换成中文处理办法。原始异常仍由调用方
    完整写入日志（见 call_verify 的 _log.exception），不会丢排查线索。
    """
    if "socksio" in str(e):
        return SOCKS_HINT
    return str(e)


def _plugin_name(module: Any) -> str:
    """取插件的可读标识（用于日志），去掉内部模块名前缀。"""
    return getattr(module, "__name__", "?").replace("_vault_plugin_", "")


@dataclass
class VerifyResult:
    """插件验证结果"""
    status: str                          # valid / invalid / error
    message: str = ""
    account_info: Optional[dict] = None  # 账号名、组织、余额等

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "account_info": self.account_info,
        }


@dataclass
class FieldSchema:
    """插件字段定义"""
    key: str
    label: str
    type: str = "text"         # text / password / textarea / select / number / url / checkbox
    required: bool = False
    sensitive: bool = False
    default: Any = None
    placeholder: str = ""
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "sensitive": self.sensitive,
        }
        if self.default is not None:
            d["default"] = self.default
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.options:
            d["options"] = self.options
        return d


@dataclass
class PluginManifest:
    """插件 manifest.json 解析结果"""
    name: str
    version: str
    credential_type: str
    author: str = ""
    description: str = ""
    icon: str = "🔑"
    fields: list[FieldSchema] = field(default_factory=list)
    # 是否允许参与批量验证。无法自动化验证或自动化验证有风险（如会产生写副作用）的
    # 插件应设为 False：前端将其排除在批量范围外，仅允许手动单个验证。
    batch_verify: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        fields = []
        for f in data.get("fields", []):
            fields.append(FieldSchema(
                key=f["key"],
                label=f["label"],
                type=f.get("type", "text"),
                required=f.get("required", False),
                sensitive=f.get("sensitive", False),
                default=f.get("default"),
                placeholder=f.get("placeholder", ""),
                options=f.get("options", []),
            ))
        return cls(
            name=data["name"],
            version=data["version"],
            credential_type=data["credential_type"],
            author=data.get("author", ""),
            description=data.get("description", ""),
            icon=data.get("icon", "🔑"),
            fields=fields,
            batch_verify=bool(data.get("batch_verify", True)),
        )

    def fields_schema_json(self) -> list[dict]:
        return [f.to_dict() for f in self.fields]


def load_manifest(manifest_path: Path) -> PluginManifest:
    """读取并校验 manifest.json"""
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    required_keys = {"name", "version", "credential_type", "fields"}
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"manifest.json 缺少必要字段: {missing}")
    return PluginManifest.from_dict(data)


def load_plugin_module(plugin_path: Path, credential_type: str) -> Any:
    """
    动态加载插件模块
    plugin_path: plugin.py 所在目录
    credential_type: 用作模块名避免冲突
    """
    py_file = plugin_path / "plugin.py"
    if not py_file.exists():
        raise FileNotFoundError(f"插件文件不存在: {py_file}")

    module_name = f"_vault_plugin_{credential_type}"
    spec = importlib.util.spec_from_file_location(module_name, str(py_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件: {py_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # 允许插件导入自身目录与类别父目录下的共享模块
    # （如 plugins/llm/deepseek/plugin.py 导入 plugins/llm/llm_common.py）。
    # 仅在执行期间临时注入 sys.path，避免污染全局模块搜索路径。
    extra_paths = [str(plugin_path), str(plugin_path.parent)]
    added = [p for p in extra_paths if p not in sys.path]
    sys.path[:0] = added
    try:
        spec.loader.exec_module(module)
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass

    if not hasattr(module, "verify"):
        raise AttributeError(f"插件 {credential_type} 缺少必要函数 verify(data)")

    return module


def call_verify(module: Any, data: dict) -> VerifyResult:
    """调用插件的 verify 函数，捕获异常"""
    name = _plugin_name(module)
    try:
        result = module.verify(data)
        if isinstance(result, VerifyResult):
            return result
        # 兼容插件自定义的 VerifyResult dataclass（鸭子类型）
        if hasattr(result, "status"):
            return VerifyResult(
                status=getattr(result, "status", "error"),
                message=getattr(result, "message", ""),
                account_info=getattr(result, "account_info", None),
            )
        # 兼容直接返回 dict 的插件
        if isinstance(result, dict):
            return VerifyResult(**result)
        _log.error("插件 %s 的 verify 返回了非预期类型: %s", name, type(result))
        return VerifyResult(status="error", message=f"插件返回了非预期类型: {type(result)}")
    except Exception as e:
        # 插件内部异常必须留痕：否则只会以 status=error 的形式回到界面，
        # 日志里查不到任何线索（排查线上证书/网络问题时的常见盲区）。
        _log.exception("插件 %s 的 verify 执行异常: %s", name, e)
        return VerifyResult(status="error", message=f"插件执行异常: {_friendly_error(e)}")


def call_validate(module: Any, data: dict) -> list[str]:
    """调用插件的可选 validate 函数，返回错误列表（空=通过）"""
    if not hasattr(module, "validate"):
        return []
    try:
        errors = module.validate(data)
        return errors if isinstance(errors, list) else []
    except Exception as e:
        _log.exception("插件 %s 的 validate 执行异常: %s", _plugin_name(module), e)
        return [f"validate 异常: {e}"]


def call_display_summary(module: Any, data: dict) -> str:
    """调用插件的可选 get_display_summary 函数"""
    if not hasattr(module, "get_display_summary"):
        return ""
    try:
        return str(module.get_display_summary(data))
    except Exception as e:
        # 仅影响列表「附加信息」列的展示，降为 warning 不打断主流程
        _log.warning("插件 %s 的 get_display_summary 执行异常: %s",
                     _plugin_name(module), e)
        return ""


# ── 可选「简易操作」接口 ──────────────────────────────────
# 插件可选实现：
#   actions(data) -> list[dict]      声明可用操作，每项 {"id","name",...}
#   run_action(action_id, params, data) -> dict   执行操作，返回 {"ok", "output", "error"}

def call_actions(module: Any, data: dict) -> list:
    """调用插件的可选 actions(data)；未实现或异常时返回空列表。"""
    if not hasattr(module, "actions"):
        return []
    try:
        result = module.actions(data or {})
        return result if isinstance(result, list) else []
    except Exception as e:
        _log.warning("插件 %s 的 actions 执行异常: %s", _plugin_name(module), e)
        return []


def call_run_action(module: Any, action_id: str, params: dict, data: dict) -> dict:
    """调用插件的可选 run_action(...)；保证返回 dict 且不抛异常。"""
    if not hasattr(module, "run_action"):
        return {"ok": False, "error": "该插件不支持操作"}
    try:
        result = module.run_action(action_id, params or {}, data or {})
        return result if isinstance(result, dict) else {"ok": False, "error": "插件返回了非预期结果"}
    except Exception as e:
        _log.exception("插件 %s 的操作 %s 执行异常: %s", _plugin_name(module), action_id, e)
        return {"ok": False, "error": f"操作失败: {_friendly_error(e)}"}


def call_list_models(module: Any, data: dict) -> list:
    """调用插件的可选 list_models(data)：返回可用模型名列表。

    未实现、取列表失败或异常时返回空列表（前端回退为手动输入）。
    """
    if not hasattr(module, "list_models"):
        return []
    try:
        result = module.list_models(data or {})
        return [str(x) for x in result] if isinstance(result, list) else []
    except Exception as e:
        # 取模型列表失败会让前端对话框回退为手动输入，用户看不到原因，必须留痕
        _log.warning("插件 %s 的 list_models 执行异常: %s", _plugin_name(module), e)
        return []


def call_stream_action(module: Any, action_id: str, params: dict, data: dict, on_delta) -> dict:
    """流式执行插件的操作；插件未实现 `stream_action` 时回退到 `run_action`（无分段）。"""
    if not hasattr(module, "stream_action"):
        return call_run_action(module, action_id, params, data)
    try:
        result = module.stream_action(action_id, params or {}, data or {}, on_delta)
        return result if isinstance(result, dict) else {"ok": False, "error": "插件返回了非预期结果"}
    except Exception as e:
        _log.exception("插件 %s 的流式操作 %s 执行异常: %s",
                       _plugin_name(module), action_id, e)
        return {"ok": False, "error": f"操作失败: {_friendly_error(e)}"}
