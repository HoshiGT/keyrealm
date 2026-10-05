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
from . import proxy_store
from .paths import app_dir
from .plugin_base import (
    VerifyResult,
    call_actions,
    call_capabilities,
    call_display_summary,
    call_list_models,
    call_run_action,
    call_stream_action,
    call_validate,
    call_verify,
    load_manifest,
    load_plugin_module,
)

_log = logger.get_logger(__name__)

# 应用根目录：开发时=项目根；冻结后=exe 同级目录。
# plugins/ 是插件的【唯一事实来源】，不打包进 exe，随 exe 同级以外部目录形式提供。
BASE_DIR = app_dir()
PLUGINS_DIR = BASE_DIR / "plugins"
# 内置插件纯净源（随发行包提供一份）：插件管理里「添加」时从这里恢复到 plugins/
BUILTIN_SOURCE_DIR = BASE_DIR / "plugins_builtin"

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
        cat_file = PLUGINS_DIR / category / "category.json"
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
            .relative_to(PLUGINS_DIR.resolve())
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
    root = PLUGINS_DIR
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


def _safe_rmtree(target: Path, root: Path) -> bool:
    """仅当 target 位于 root 之内时删除，防止路径越界误删。"""
    try:
        t = target.resolve()
        r = root.resolve()
        if t != r and t.is_relative_to(r) and t.is_dir():
            shutil.rmtree(t)
            return True
    except Exception as e:
        _log.warning("删除插件目录失败 %s: %s", target, e)
    return False


def delete_plugin(credential_type: str, delete_credentials: bool = True) -> dict:
    """删除一个插件：移除其 plugins/ 目录、卸载模块、删除插件记录。

    delete_credentials=True（默认）时一并删除该类型下的全部凭据。
    """
    record = db.get_plugin_by_type(credential_type)
    if not record:
        return {"ok": False, "message": "插件不存在"}

    name = record.get("name") or credential_type
    removed = 0
    if delete_credentials:
        for cred in db.list_credentials(credential_type=credential_type):
            db.delete_credential(cred["id"])
            removed += 1

    _unload_plugin(credential_type)
    db.delete_plugin_record(credential_type)
    # 目录删除放在最后：即便删除失败，记录也已清理，重启扫描时不会被再次加载
    deleted_dir = _safe_rmtree(Path(record["plugin_path"]), PLUGINS_DIR)

    _log.info("插件已删除 %s（凭据 %d 条，目录删除=%s）", credential_type, removed, deleted_dir)
    return {"ok": True, "name": name, "deleted_credentials": removed, "deleted_dir": deleted_dir}


def list_available_plugins() -> list[dict]:
    """列出内置纯净源里、当前尚未安装（或已被删除）的插件。"""
    installed = {p["credential_type"] for p in db.list_plugins()}
    available: list[dict] = []
    if not BUILTIN_SOURCE_DIR.exists():
        return available
    for cat_dir in sorted(BUILTIN_SOURCE_DIR.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith(("_", ".")):
            continue
        cat_meta: dict = {}
        cat_file = cat_dir / "category.json"
        if cat_file.exists():
            try:
                data = json.loads(cat_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cat_meta = data
            except Exception:
                pass
        for sub in sorted(cat_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith(("_", ".")):
                continue
            manifest_file = sub / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                manifest = load_manifest(manifest_file)
            except Exception:
                continue
            if manifest.credential_type in installed:
                continue
            available.append({
                "category": cat_dir.name,
                "category_name": _category_display(cat_dir.name, cat_meta),
                # 与「已安装」列表保持一致的展示字段：缺这两个的话，可添加卡片的类别标题
                # 只能退到子插件 emoji、单条也没有 logo，两张卡片看起来像两套设计
                "category_icon": cat_meta.get("icon", ""),
                "icon_image": get_icon_image({
                    "icon": manifest.icon,
                    "plugin_path": str(sub),
                    "credential_type": manifest.credential_type,
                }),
                "service": sub.name,
                "credential_type": manifest.credential_type,
                "name": manifest.name,
                "icon": manifest.icon,
                "description": manifest.description,
            })
    return available


def install_plugin(category: str, service: str) -> dict:
    """从内置纯净源把某个插件复制回 plugins/ 并同步加载。"""
    src = BUILTIN_SOURCE_DIR / category / service
    if not (src / "manifest.json").exists():
        return {"ok": False, "message": "内置源中未找到该插件"}
    dest = PLUGINS_DIR / category / service
    if dest.exists():
        return {"ok": False, "message": "该插件已存在"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 连同类别元数据一并补齐（若目标类别目录尚无 category.json）
        cat_src = BUILTIN_SOURCE_DIR / category / "category.json"
        cat_dest = PLUGINS_DIR / category / "category.json"
        if cat_src.exists() and not cat_dest.exists():
            shutil.copy2(cat_src, cat_dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except Exception as e:
        _log.error("安装插件失败 %s/%s: %s", category, service, e)
        return {"ok": False, "message": f"安装失败: {e}"}
    sync_plugins()
    return {"ok": True}


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
    """调用插件验证凭据（含「按凭据解析代理」与「欠费探测」）"""
    module = get_module(credential_type)
    if module is None:
        return VerifyResult(status="error", message=f"插件未安装或加载失败: {credential_type}")
    # 代理作用域收口在这里：单条 / 批量 / MCP / 定时 四条路径都经此函数，
    # 因此各条凭据的「用哪个代理」自动生效且线程隔离（见 proxy_store）。
    with proxy_store.credential_proxy_scope(data, credential_type):
        result = call_verify(module, data)
        _apply_quota_probe(credential_type, module, data, result)
    return result


def plugin_uses_proxy(credential_type: str) -> bool:
    """该插件的请求是否走代理（决定前端要不要给它显示「使用代理」）。

    判据是「有没有 ``make_client``」——走 httpx 的类别才有；``mail_smtp`` 用标准库
    ``smtplib``，本就不支持 HTTP 代理，给它显示代理选项只会误导。
    """
    module = get_module(credential_type)
    return bool(module is not None and callable(getattr(module, "make_client", None)))


def _apply_quota_probe(credential_type: str, module, data: dict, result: VerifyResult) -> None:
    """余额拿不到时的「欠费探测」后置检查（原地修改 result）。

    只在**验证通过**且**没有余额/使用率**时才给插件机会——有余额就没必要再花一发请求。
    开关与模型都在凭据自己的字段里（``probe_chat`` / ``probe_model``），未接入探测的
    插件没有 ``quota_probe``，直接跳过。
    """
    if result.status != "valid":
        return
    info = result.account_info or {}
    if info.get("balance") is not None or info.get("usage_rate") is not None:
        return
    probe = getattr(module, "quota_probe", None)
    if not callable(probe):
        return
    try:
        extra = probe(data)
    except Exception as e:  # noqa: BLE001 - 后置检查绝不能影响验证结果
        _log.warning("欠费探测异常 %s: %s", credential_type, type(e).__name__)
        return
    if not isinstance(extra, dict):
        return
    extra_info = extra.get("account_info")
    if isinstance(extra_info, dict) and extra_info:
        result.account_info = {**info, **extra_info}
    if extra.get("message"):
        result.message = str(extra["message"])


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


def has_actions(credential_type: str) -> bool:
    """该插件是否声明了「简易操作」（可选能力）。"""
    module = get_module(credential_type)
    return bool(module is not None and hasattr(module, "actions"))


def plugin_actions(credential_type: str, data: dict) -> list:
    """该插件对给定凭据可用的操作列表。"""
    module = get_module(credential_type)
    return call_actions(module, data) if module else []


def plugin_capabilities(credential_type: str, data: dict) -> dict:
    """该凭据当前真正能用的操作（可选能力）。

    **会联网**（插件要拿凭据去打上游接口探权限），所以包在代理作用域里，与 `plugin_models` 同理。
    """
    module = get_module(credential_type)
    if module is None:
        return {"available": None, "note": "", "error": True}
    with proxy_store.credential_proxy_scope(data, credential_type):
        return call_capabilities(module, data)


def run_plugin_action(credential_type: str, action_id: str, params: dict, data: dict) -> dict:
    """执行插件的某个操作。"""
    module = get_module(credential_type)
    if module is None:
        return {"ok": False, "error": "插件未安装或加载失败"}
    with proxy_store.credential_proxy_scope(data, credential_type):
        return call_run_action(module, action_id, params, data)


def plugin_models(credential_type: str, data: dict) -> list:
    """该插件对给定凭据可用的模型列表（可选能力）。"""
    module = get_module(credential_type)
    if module is None:
        return []
    with proxy_store.credential_proxy_scope(data, credential_type):
        return call_list_models(module, data)


def stream_plugin_action(credential_type: str, action_id: str, params: dict, data: dict,
                         on_delta) -> dict:
    """流式执行插件操作；插件未实现时回退为普通执行。

    注意：调用方（``api.stream_credential_action``）是在**新线程**里跑这个函数的，
    所以代理作用域必须在这里建立——设在调用线程里子线程读不到。
    """
    module = get_module(credential_type)
    if module is None:
        return {"ok": False, "error": "插件未安装或加载失败"}
    with proxy_store.credential_proxy_scope(data, credential_type):
        return call_stream_action(module, action_id, params, data, on_delta)


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
