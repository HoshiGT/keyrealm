"""
插件管理器：plugins/ 目录两层扫描，自动加载

目录结构（一服务一插件，同类插件可按类别目录归组）：

    plugins/
      llm/                    # 类别目录（对应左侧树的一层）
        category.json         # 可选：{"name": "LLM 模型", "icon": "🤖"}
        llm_common.py         # 可选：子插件共享代码
        deepseek/             # 独立子插件
          manifest.json
          plugin.py
        openai/...
      some_plugin/            # 也支持根级平铺插件（无类别）
        manifest.json
        plugin.py

放入插件目录即自动加载，删除目录即在下次扫描时自动卸载，无需手动管理。
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
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

# 应用根目录：开发时=项目根；冻结后=exe 同级目录。
# plugins/ 是插件的【唯一事实来源】，不打包进 exe，随 exe 同级以外部目录形式提供。
BASE_DIR = app_dir()
BUILTIN_PLUGINS_DIR = BASE_DIR / "plugins"

# 旧版本运行期缓存目录（zip 安装时代），启动时一次性迁移/清理
_LEGACY_PLUGINS_DIR = BASE_DIR / "data" / "installed_plugins"

# 旧版内置整包插件的 credential_type（其缓存目录直接清理，不迁移）
_LEGACY_BUILTIN_TYPES = ("llm_api_key", "api_service")

# 内存中的插件模块缓存  credential_type -> module
_loaded_modules: dict[str, Any] = {}

# manifest icon 字段支持图片文件名（相对于插件目录），解析为 data URL 后的缓存
_ICON_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".gif": "image/gif",
}
_icon_image_cache: dict[str, Optional[str]] = {}

# 类别目录 category.json 的 icon 缓存（category_folder -> icon）
_category_icon_cache: dict[str, Optional[str]] = {}


def get_category_icon(category: str) -> Optional[str]:
    """类别图标：读取 <类别目录>/category.json 的 icon 字段。

    类别节点与插件节点图标相互独立，避免类别借用某个插件的 icon。
    未配置时返回 None，前端回退到默认文件夹图标。
    """
    if not category:
        return None
    if category in _category_icon_cache:
        return _category_icon_cache[category]
    result: Optional[str] = None
    try:
        cat_file = BUILTIN_PLUGINS_DIR / category / "category.json"
        if cat_file.is_file():
            with open(cat_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("icon"):
                result = str(data["icon"])
    except Exception as e:
        _log.warning("类别图标读取失败 %s: %s", category, e)
    _category_icon_cache[category] = result
    return result


def _plugin_rel_path(plugin_path: str) -> str:
    """插件目录相对 plugins/ 的路径（如 llm/deepseek），供前端作为提示展示。"""
    if not plugin_path:
        return ""
    try:
        return (
            Path(plugin_path).resolve()
            .relative_to(BUILTIN_PLUGINS_DIR.resolve())
            .as_posix()
        )
    except Exception:
        return Path(plugin_path).name


def get_icon_image(record: dict) -> Optional[str]:
    """插件自定义图标：manifest 的 icon 为插件目录内图片文件时读取为 data URL。

    icon 不是图片文件名（如 emoji）时返回 None，前端回退到默认展示。
    """
    icon = (record.get("icon") or "").strip()
    ext = Path(icon).suffix.lower()
    if not icon or ext not in _ICON_IMAGE_MIME:
        return None
    ct = record.get("credential_type", "")
    if ct in _icon_image_cache:
        return _icon_image_cache[ct]
    result: Optional[str] = None
    try:
        plugin_dir = Path(record["plugin_path"]).resolve()
        path = (plugin_dir / icon).resolve()
        # 防目录穿越：图标文件必须位于插件目录内
        if path.is_relative_to(plugin_dir) and path.is_file():
            b64 = base64.b64encode(path.read_bytes()).decode()
            result = f"data:{_ICON_IMAGE_MIME[ext]};base64,{b64}"
    except Exception as e:
        _log.warning("插件图标读取失败 %s: %s", ct, e)
    _icon_image_cache[ct] = result
    return result


def _scan_plugin_dirs() -> list[tuple[Path, str, dict]]:
    """扫描 plugins/ 两层结构。

    返回 [(plugin_dir, category_folder, category_meta)]：
    - 根级含 manifest.json 的目录为平铺插件（category 为空）；
    - 否则视为类别目录，其下含 manifest.json 的子目录为子插件，
      category.json 提供类别显示名/图标。
    """
    results: list[tuple[Path, str, dict]] = []
    root = BUILTIN_PLUGINS_DIR
    if not root.exists():
        return results
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        if (entry / "manifest.json").exists():
            results.append((entry, "", {}))
            continue
        cat_meta: dict = {}
        cat_file = entry / "category.json"
        if cat_file.exists():
            try:
                with open(cat_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cat_meta = data
            except Exception as e:
                _log.warning("类别元数据读取失败 %s: %s", cat_file, e)
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir() or sub.name.startswith(("_", ".")):
                continue
            if (sub / "manifest.json").exists():
                results.append((sub, entry.name, cat_meta))
    return results


def _category_display(category: str, meta: dict) -> str:
    return str(meta.get("name") or category or "未分类")


def sync_plugins():
    """扫描 plugins/ 目录，同步插件注册表与已加载模块。

    - 新目录 → 注册并加载（放入即生效）；
    - 已存在目录 → 更新记录并重载模块（改完重启/刷新即生效）；
    - 磁盘上已消失的目录 → 移除模块与数据库记录（删除即卸载，凭据保留）。
    """
    _icon_image_cache.clear()
    _category_icon_cache.clear()
    found: set[str] = set()
    for plugin_dir, category, cat_meta in _scan_plugin_dirs():
        try:
            manifest = load_manifest(plugin_dir / "manifest.json")
        except Exception as e:
            _log.error("插件 manifest 无效 %s: %s", plugin_dir, e)
            continue
        ct = manifest.credential_type
        found.add(ct)
        try:
            db.upsert_plugin(
                name=manifest.name,
                version=manifest.version,
                credential_type=ct,
                fields_schema=manifest.fields_schema_json(),
                plugin_path=str(plugin_dir),
                author=manifest.author,
                description=manifest.description,
                icon=manifest.icon,
                is_builtin=True,
                category=category,
                category_name=_category_display(category, cat_meta),
                batch_verify=manifest.batch_verify,
            )
            load_plugin(ct)
        except Exception as e:
            _log.error("插件加载失败 %s (%s): %s", plugin_dir, ct, e)

    for record in db.list_plugins():
        ct = record["credential_type"]
        if ct in found:
            continue
        _unload_plugin(ct)
        db.delete_plugin_record(ct)
        _log.info("插件已移除（目录不存在）: %s", ct)


def _unload_plugin(credential_type: str):
    _loaded_modules.pop(credential_type, None)
    sys.modules.pop(f"_vault_plugin_{credential_type}", None)


def migrate_legacy_installed_plugins():
    """迁移旧版本 data/installed_plugins/ 运行期缓存。

    旧内置整包插件（llm_api_key/api_service）的缓存直接删除；
    用户 zip 安装的其他插件移动到 plugins/ 根级，纳入自动加载。
    """
    legacy = _LEGACY_PLUGINS_DIR
    if not legacy.exists():
        return
    for entry in sorted(legacy.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in _LEGACY_BUILTIN_TYPES or not (entry / "manifest.json").exists():
            shutil.rmtree(entry, ignore_errors=True)
            continue
        dest = BUILTIN_PLUGINS_DIR / entry.name
        if dest.exists():
            shutil.rmtree(entry, ignore_errors=True)
            continue
        try:
            BUILTIN_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(dest))
            _log.info("旧版插件已迁移到 plugins/: %s", entry.name)
        except Exception as e:
            _log.error("旧版插件迁移失败 %s: %s", entry.name, e)
    try:
        legacy.rmdir()  # 仅清空后删除目录本身
    except OSError:
        pass


def install_builtin_plugins():
    """启动入口：迁移旧缓存并全量扫描 plugins/（保留旧函数名以便调用方兼容）。"""
    migrate_legacy_installed_plugins()
    sync_plugins()


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
    _unload_plugin(credential_type)
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
    """列出所有插件（含凭据数量）；先做一次目录扫描同步，使新放入的插件即时可见"""
    sync_plugins()
    plugins = db.list_plugins()
    for p in plugins:
        creds = db.list_credentials(credential_type=p["credential_type"])
        p["credential_count"] = len(creds)
        p["category_icon"] = get_category_icon(p.get("category") or "")
        p["rel_path"] = _plugin_rel_path(p.get("plugin_path") or "")
    return plugins
