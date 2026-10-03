"""
插件基类与接口定义
所有插件必须实现 verify(data: dict) -> VerifyResult
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


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
    spec.loader.exec_module(module)

    if not hasattr(module, "verify"):
        raise AttributeError(f"插件 {credential_type} 缺少必要函数 verify(data)")

    return module


def call_verify(module: Any, data: dict) -> VerifyResult:
    """调用插件的 verify 函数，捕获异常"""
    try:
        result = module.verify(data)
        if isinstance(result, VerifyResult):
            return result
        # 兼容直接返回 dict 的插件
        if isinstance(result, dict):
            return VerifyResult(**result)
        return VerifyResult(status="error", message=f"插件返回了非预期类型: {type(result)}")
    except Exception as e:
        return VerifyResult(status="error", message=f"插件执行异常: {e}")


def call_validate(module: Any, data: dict) -> list[str]:
    """调用插件的可选 validate 函数，返回错误列表（空=通过）"""
    if not hasattr(module, "validate"):
        return []
    try:
        errors = module.validate(data)
        return errors if isinstance(errors, list) else []
    except Exception as e:
        return [f"validate 异常: {e}"]


def call_display_summary(module: Any, data: dict) -> str:
    """调用插件的可选 get_display_summary 函数"""
    if not hasattr(module, "get_display_summary"):
        return ""
    try:
        return str(module.get_display_summary(data))
    except Exception:
        return ""
