"""
插件管理器
负责 .zip 插件包的安装、卸载、加载、热重载
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from . import database as db
from . import logger
from .paths import app_dir
from .plugin_base import (
    PluginManifest,
    VerifyResult,
    call_display_summary,
    call_validate,
    call_verify,
    load_manifest,
    load_plugin_module,
)

_log = logger.get_logger(__name__)

# 应用根目录：开发时=项目根；冻结后=exe 同级目录
# plugins/（内置插件，用户可改）与 data/installed_plugins/（运行时安装）都在此目录下，
# 均不打包进 exe，随 exe 同级以外部目录形式提供。
BASE_DIR = app_dir()
PLUGINS_DIR = BASE_DIR / "data" / "installed_plugins"
BUILTIN_PLUGINS_DIR = BASE_DIR / "plugins"

# 内存中的插件模块缓存  credential_type -> module
_loaded_modules: dict[str, Any] = {}


def _ensure_dirs():
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)


def install_builtin_plugins():
    """启动时安装内置插件（plugins/ 目录下的）"""
    _ensure_dirs()
    if not BUILTIN_PLUGINS_DIR.exists():
        return
    for plugin_dir in BUILTIN_PLUGINS_DIR.iterdir():
        if not plugin_dir.is_dir():
            continue
        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_manifest(manifest_path)
            # 复制到 installed_plugins
            dest = PLUGINS_DIR / manifest.credential_type
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(plugin_dir, dest)
            db.upsert_plugin(
                name=manifest.name,
                version=manifest.version,
                credential_type=manifest.credential_type,
                fields_schema=manifest.fields_schema_json(),
                plugin_path=str(dest),
                author=manifest.author,
                description=manifest.description,
                icon=manifest.icon,
                is_builtin=True,
            )
            load_plugin(manifest.credential_type)
        except Exception as e:
            _log.error("内置插件安装失败 %s: %s", plugin_dir.name, e)


def install_from_zip(zip_path: str) -> tuple[bool, str]:
    """
    从 .zip 文件安装插件
    返回 (success, message)
    """
    _ensure_dirs()
    zip_file = Path(zip_path)
    if not zip_file.exists():
        return False, f"文件不存在: {zip_path}"
    if not zip_file.suffix.lower() == ".zip":
        return False, "仅支持 .zip 格式插件包"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            with zipfile.ZipFile(zip_file, "r") as zf:
                # 安全检查：防止路径穿越
                for name in zf.namelist():
                    if name.startswith("/") or ".." in name:
                        return False, f"插件包包含不安全路径: {name}"
                zf.extractall(tmp)
        except zipfile.BadZipFile:
            return False, "无效的 zip 文件"

        # 找到 manifest.json（可能在根目录或子目录）
        manifest_path = _find_manifest(tmp)
        if manifest_path is None:
            return False, "插件包中未找到 manifest.json"

        plugin_dir = manifest_path.parent
        py_file = plugin_dir / "plugin.py"
        if not py_file.exists():
            return False, "插件包中未找到 plugin.py"

        try:
            manifest = load_manifest(manifest_path)
        except ValueError as e:
            return False, str(e)

        # 检查是否与内置插件冲突
        existing = db.get_plugin_by_type(manifest.credential_type)
        if existing and existing.get("is_builtin"):
            return False, f"不能覆盖内置插件: {manifest.credential_type}"

        # 复制到 installed_plugins
        dest = PLUGINS_DIR / manifest.credential_type
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(plugin_dir, dest)

        # 写入数据库
        db.upsert_plugin(
            name=manifest.name,
            version=manifest.version,
            credential_type=manifest.credential_type,
            fields_schema=manifest.fields_schema_json(),
            plugin_path=str(dest),
            author=manifest.author,
            description=manifest.description,
            icon=manifest.icon,
            is_builtin=False,
        )

        # 热加载
        try:
            load_plugin(manifest.credential_type)
        except Exception as e:
            return True, f"插件已安装但加载失败（重启后生效）: {e}"

        return True, f"插件 {manifest.name} v{manifest.version} 安装成功"


def _find_manifest(root: Path) -> Optional[Path]:
    """在解压目录中找 manifest.json"""
    direct = root / "manifest.json"
    if direct.exists():
        return direct
    for p in root.rglob("manifest.json"):
        return p
    return None


def uninstall_plugin(credential_type: str) -> tuple[bool, str]:
    """卸载插件（保留凭据，仅禁用）"""
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return False, "插件不存在"
    if record.get("is_builtin"):
        return False, "内置插件不可卸载"

    # 从内存移除
    _loaded_modules.pop(credential_type, None)

    # 删除文件
    plugin_path = Path(record["plugin_path"])
    if plugin_path.exists():
        shutil.rmtree(plugin_path, ignore_errors=True)

    # 数据库：禁用而非删除（保留凭据关联）
    db.set_plugin_enabled(credential_type, False)
    db.delete_plugin_record(credential_type)

    return True, f"插件 {record['name']} 已卸载"


def load_plugin(credential_type: str) -> Optional[Any]:
    """加载插件模块到内存"""
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return None
    plugin_path = Path(record["plugin_path"])
    try:
        module = load_plugin_module(plugin_path, credential_type)
        _loaded_modules[credential_type] = module
        return module
    except Exception as e:
        _log.error("加载插件失败 %s: %s", credential_type, e)
        return None


def reload_plugin(credential_type: str) -> Optional[Any]:
    """热重载插件"""
    _loaded_modules.pop(credential_type, None)
    return load_plugin(credential_type)


def get_module(credential_type: str) -> Optional[Any]:
    """获取已加载的插件模块（懒加载）"""
    if credential_type not in _loaded_modules:
        return load_plugin(credential_type)
    return _loaded_modules[credential_type]


def verify_credential(credential_type: str, data: dict) -> VerifyResult:
    """调用插件验证凭据"""
    module = get_module(credential_type)
    if module is None:
        return VerifyResult(status="error", message=f"插件未安装或加载失败: {credential_type}")
    return call_verify(module, data)


def validate_credential(credential_type: str, data: dict) -> list[str]:
    """调用插件校验字段"""
    module = get_module(credential_type)
    if module is None:
        return [f"插件未安装: {credential_type}"]
    return call_validate(module, data)


def get_display_summary(credential_type: str, data: dict) -> str:
    """调用插件获取显示摘要"""
    module = get_module(credential_type)
    if module is None:
        return ""
    return call_display_summary(module, data)


def list_installed_plugins() -> list[dict]:
    """列出所有已安装插件（含凭据数量）"""
    plugins = db.list_plugins()
    for p in plugins:
        creds = db.list_credentials(credential_type=p["credential_type"])
        p["credential_count"] = len(creds)
    return plugins
