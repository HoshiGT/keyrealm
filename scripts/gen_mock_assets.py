#!/usr/bin/env python3
"""生成「开发模式 Mock」用的两份数据文件（**产物勿手工编辑**）。

产物
----
- ``frontend/src/api/pluginIcons.js`` —— ``<credential_type> → logo data URL``
- ``frontend/src/api/mockPlugins.js`` —— 插件目录，与 ``plugins/`` 目录逐条一致

为什么需要它
------------
浏览器里没有后端、也没有 ``plugins/`` 可扫，所以 ``frontend/src/api/mock.js`` 需要一份
**离线快照**撑起插件管理页、凭据页目录树、新建类型下拉等一切依赖插件元数据的界面。
早先这两份数据是手写的示意样本，很快与真实目录漂了（漏插件、编造插件、类别放错）。

本脚本从 ``plugins/`` 现扫现生成，让 dev 模式与真实目录一致。**加了/改了插件就重跑一次**：

    .venv/bin/python scripts/gen_mock_assets.py

输出刻意保持**确定性**（不写时间戳、按 credential_type 排序、id 顺序分配），
这样没改插件时重跑不会产生 diff。
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
OUT_DIR = ROOT / "frontend" / "src" / "api"

# 位图统一压到这个边长再内嵌（渲染位最大也就 22px，原始 128² 没必要原样塞进来）
RASTER_EDGE = 64

sys.path.insert(0, str(ROOT))

from backend import icon_ink                      # noqa: E402
from backend.plugin_base import load_plugin_module, plain_text  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _enabled_map() -> dict[str, bool]:
    """``plugins/.state.json`` 的启用状态；文件不存在 = 全部启用（与后端同一约定）。"""
    state = PLUGINS / ".state.json"
    if not state.is_file():
        return {}
    data = _read_json(state)
    disabled = data.get("disabled") if isinstance(data, dict) else None
    if isinstance(disabled, list):
        return {str(t): False for t in disabled}
    # 兼容 {credential_type: bool} 形态
    if isinstance(data, dict):
        return {k: bool(v) for k, v in data.items() if isinstance(v, bool)}
    return {}


def _icon_data_url(path: Path) -> str:
    """与后端 ``plugin_manager.get_icon_image()`` 同源，但位图先压到 64px 以控体积。

    - SVG 直接内嵌原文（无损，且体积本来就只有几十 KB 量级）；
    - 位图用 Pillow 缩到 ``RASTER_EDGE`` 后重编码成 PNG（保 alpha）。
    """
    raw = path.read_bytes()
    if path.suffix.lower() == ".svg":
        return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")

    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGBA")
        im.thumbnail((RASTER_EDGE, RASTER_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def collect() -> tuple[list[dict], dict[str, str]]:
    """返回 ``(插件目录, credential_type → data URL)``。"""
    enabled = _enabled_map()
    plugins: list[dict] = []
    icons: dict[str, str] = {}

    # 类别目录 + 根级平铺插件（后者 category 为空）
    for cat_dir in sorted(p for p in PLUGINS.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        cat_conf = cat_dir / "category.json"
        if cat_conf.is_file():
            conf = _read_json(cat_conf)
            category, cat_name, cat_icon = cat_dir.name, conf.get("name") or cat_dir.name, conf.get("icon") or ""
        else:
            # 根级平铺插件：目录本身就是插件
            category, cat_name, cat_icon = "", "", ""
            if (cat_dir / "manifest.json").is_file():
                _add(plugins, icons, cat_dir, category, cat_name, cat_icon, enabled)
            continue
        for svc_dir in sorted(d for d in cat_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))):
            if (svc_dir / "manifest.json").is_file():
                _add(plugins, icons, svc_dir, category, cat_name, cat_icon, enabled)

    plugins.sort(key=lambda p: p["credential_type"])
    for i, p in enumerate(plugins, start=1):
        p["id"] = i
    return plugins, icons


def _add(plugins, icons, svc_dir, category, cat_name, cat_icon, enabled) -> None:
    mf = _read_json(svc_dir / "manifest.json")
    ct = mf["credential_type"]
    icon = str(mf.get("icon") or "")

    icon_image = ""
    icon_path = svc_dir / icon
    if icon and "://" not in icon and icon_path.is_file():
        icon_image = _icon_data_url(icon_path)
        icons[ct] = icon_image

    # has_actions 直接问插件本体，而不是扫源码猜（与后端 plugin_manager.has_actions 同口径）
    try:
        module = load_plugin_module(svc_dir, ct)
        has_actions = hasattr(module, "actions")
    except Exception as exc:  # noqa: BLE001 - 加载不了就如实报，不要静默当成「没有操作」
        print(f"  ! {ct} 加载失败，has_actions 记 False：{exc}", file=sys.stderr)
        has_actions = False

    plugins.append({
        "id": 0,                                  # 收集完再统一分配
        "name": mf.get("name") or svc_dir.name,
        "version": mf.get("version") or "1.0.0",
        "author": mf.get("author") or "Keyrealm",
        # 前端不渲染 markdown：与生产下发同口径剥掉 `**加粗**`（见 backend/plugin_base.plain_text）
        "description": plain_text(mf.get("description") or ""),
        "credential_type": ct,
        "icon": icon,
        # 深色主题下要不要垫浅色底：与后端同一套判据（backend/icon_ink.py）。
        # emoji 图标（icon 不是文件名）没有判定对象，恒为 False。
        "icon_lift": bool(icon_image) and icon_ink.needs_lift(icon_path),
        "category": category,
        "category_name": cat_name,
        "category_icon": cat_icon,
        "rel_path": f"{category}/{svc_dir.name}" if category else svc_dir.name,
        "batch_verify": mf.get("batch_verify", True) is not False,
        "enabled": enabled.get(ct, True),
        "has_actions": has_actions,
        "installed_at": None,                     # 由 mock.js 填 daysAgo(30)，避免时间戳污染产物
        "credential_count": 0,                    # mock 在运行时按凭据现算
        "fields_schema": plain_text(mf.get("fields") or []),
    })


def write_icons(icons: dict[str, str]) -> None:
    lines = [
        "// 内置插件 logo（data URL）。来源与 plugins/<类别>/<服务>/ 下的实际图标文件一致；",
        "// 位图压到 64px、SVG 保留原文。由 scripts/gen_mock_assets.py 生成，勿手工编辑。",
        "// 仅供开发模式 Mock 后端离线展示；生产构建里 mock.js 被摇树移除，整份数据不会进包。",
        "export const PLUGIN_ICON_IMAGES = {",
    ]
    for ct in sorted(icons):
        lines.append(f"  '{ct}': '{icons[ct]}',")
    lines.append("}")
    lines.append("")
    (OUT_DIR / "pluginIcons.js").write_text("\n".join(lines), encoding="utf-8")


def write_plugins(plugins: list[dict]) -> None:
    lines = [
        "// 插件目录快照：与 plugins/ 目录逐条一致。",
        "// 由 scripts/gen_mock_assets.py 生成，勿手工编辑（加插件后重跑该脚本）。",
        "// 供开发模式 Mock 后端离线撑起插件管理页 / 凭据目录树 / 新建类型下拉。",
        "// 注意：不含 icon_image —— 那部分体积大且与 pluginIcons.js 重复，",
        "// 由 mock.js 按 credential_type 从 PLUGIN_ICON_IMAGES 现拼。",
        "export const MOCK_PLUGINS = [",
    ]
    for p in plugins:
        lines.append("  " + json.dumps(p, ensure_ascii=False) + ",")
    lines.append("]")
    lines.append("")
    (OUT_DIR / "mockPlugins.js").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    plugins, icons = collect()
    if not plugins:
        print("未扫描到任何插件，已中止（不会写出空产物）", file=sys.stderr)
        return 1
    write_icons(icons)
    write_plugins(plugins)

    cats = {}
    for p in plugins:
        cats[p["category"] or "(根)"] = cats.get(p["category"] or "(根)", 0) + 1
    print(f"已生成 {len(plugins)} 个插件 / {len(cats)} 个类别 / {len(icons)} 张图标")
    print("  类别分布：" + "、".join(f"{k} {v}" for k, v in sorted(cats.items())))
    print(f"  → {OUT_DIR / 'mockPlugins.js'}")
    print(f"  → {OUT_DIR / 'pluginIcons.js'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
