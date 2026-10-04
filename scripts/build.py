#!/usr/bin/env python3
"""构建 Credential Vault 单文件可执行程序，输出到 release/。

跨平台：在 Windows 上产出 release/CredentialVault.exe，在 Linux/macOS 上产出
release/CredentialVault。**要生成 Windows exe，必须在 Windows 上运行本脚本**
（PyInstaller 不能跨平台交叉编译）。

打包边界
--------
- 进 exe：后端代码 + frontend/dist（只读资源）。
- 不进 exe、以外部目录放在 release/ 下：
    · plugins/  —— 内置插件，用户可直接编辑（改完重启即生效）
    · data/     —— 运行时自动创建（数据库、已装插件、主密码校验）
  release/ 因此是一个自包含、可便携分发的目录，且不被 git 追踪。

用法
----
    python scripts/build.py               # 需要已存在 frontend/dist
    python scripts/build.py --frontend    # 先执行 npm run build 再打包
    python scripts/build.py --console     # 保留控制台窗口（调试用）
    python scripts/build.py --smoke       # 构建后跑 --print-paths 校验路径（自动开控制台）
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "CredentialVault.spec"
FRONTEND = ROOT / "frontend"
FRONTEND_DIST = FRONTEND / "dist"
RELEASE = ROOT / "release"
DIST = ROOT / "dist"      # PyInstaller 产物中转（gitignored）
WORK = ROOT / "build"     # PyInstaller 中间文件（gitignored）

IS_WINDOWS = platform.system() == "Windows"
EXE_NAME = "CredentialVault.exe" if IS_WINDOWS else "CredentialVault"


def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    """执行子进程，实时透传输出，失败即退出。"""
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n", flush=True)
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if result.returncode != 0:
        sys.exit(f"命令失败（退出码 {result.returncode}）：{' '.join(map(str, cmd))}")


def build_frontend() -> None:
    """执行 npm run build 生成 frontend/dist。"""
    if not (FRONTEND / "package.json").exists():
        sys.exit("未找到 frontend/package.json，无法构建前端")
    npm = "npm.cmd" if IS_WINDOWS else "npm"
    if shutil.which(npm) is None:
        sys.exit("未找到 npm，请先安装 Node.js，或去掉 --frontend 手动构建")
    if not (FRONTEND / "node_modules").exists():
        _run([npm, "install"], cwd=FRONTEND)
    _run([npm, "run", "build"], cwd=FRONTEND)


def run_pyinstaller(console: bool) -> None:
    """调用当前解释器的 PyInstaller，按 spec 打包到 dist/。"""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("当前 Python 环境未安装 PyInstaller：pip install pyinstaller")

    if not FRONTEND_DIST.exists():
        sys.exit(
            "未找到 frontend/dist —— 前端尚未构建。\n"
            "请先执行： cd frontend && npm install && npm run build\n"
            "或直接用： python scripts/build.py --frontend"
        )

    env = dict(os.environ)
    if console:
        env["CV_CONSOLE"] = "1"
    else:
        env.pop("CV_CONSOLE", None)

    _run(
        [
            sys.executable, "-m", "PyInstaller",
            "--clean", "--noconfirm",
            "--distpath", str(DIST),
            "--workpath", str(WORK),
            str(SPEC),
        ],
        cwd=ROOT,
        env=env,
    )

    built = DIST / EXE_NAME
    if not built.exists():
        sys.exit(f"PyInstaller 结束但未找到产物：{built}")


def assemble_release() -> Path:
    """把 dist/ 中的可执行文件与外部 plugins/ 组装进 release/。"""
    built = DIST / EXE_NAME
    if not built.exists():
        sys.exit(f"未找到构建产物：{built}")

    # 不整体清空 release/：保留其中已存在的 data/（运行时数据）与
    # credential-vault-src.zip（源码归档）等无关文件，只替换本脚本负责的 exe 与 plugins/。
    RELEASE.mkdir(parents=True, exist_ok=True)

    exe_dest = RELEASE / EXE_NAME
    if exe_dest.exists():
        exe_dest.unlink()
    shutil.copy2(built, exe_dest)

    # 外部可改插件目录：复制源码形式的 plugins/（manifest.json + plugin.py）
    # 排除 __pycache__/*.pyc，避免把构建机的字节码带进分发包
    src_plugins = ROOT / "plugins"
    dest_plugins = RELEASE / "plugins"
    if dest_plugins.exists():
        shutil.rmtree(dest_plugins)
    if src_plugins.exists():
        shutil.copytree(
            src_plugins,
            dest_plugins,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    else:
        print("警告：未找到 plugins/，release/ 将不含内置插件", file=sys.stderr)

    # 约定：plugins/ 是插件的唯一事实来源；data/installed_plugins/ 是旧版本
    # （zip 安装时代）遗留的运行期缓存，应用启动时由 migrate_legacy_installed_plugins()
    # 一次性迁移/清理，不随发行包分发。若用户此前从 release/ 运行过、留下了该缓存
    # 目录，此处精确删除它（绝不触碰 data/ 下其它内容，如 vault.db、logs/ 等）。
    stale_cache = RELEASE / "data" / "installed_plugins"
    if stale_cache.exists():
        shutil.rmtree(stale_cache)

    # data/ 其余内容不预建，应用首次运行时自动创建
    return exe_dest


def smoke_test(exe: Path) -> None:
    """运行 exe --print-paths 校验冻结后的路径解析（需控制台构建）。"""
    print("\n=== 冒烟校验：--print-paths ===", flush=True)
    result = subprocess.run([str(exe), "--print-paths"], capture_output=True, text=True, timeout=120)
    print(result.stdout, end="")
    if result.stderr:
        print("[stderr]", result.stderr, end="")
    # 校验关键路径落在 release/ 下、frontend 随包
    ok = True
    out = result.stdout
    if str(RELEASE) not in out:
        print(f"✗ app_dir / db / plugins 未指向 release/（{RELEASE}）")
        ok = False
    if "frontend_index" in out and "_MEI" not in out.split("frontend_index", 1)[1].splitlines()[0]:
        # 冻结后 frontend 应来自 _MEIPASS（临时解压目录，路径含 _MEI）
        print("✗ frontend_index 未指向包内 _MEIPASS 资源")
        ok = False
    print("冒烟校验：", "通过 ✓" if ok else "存在问题 ✗")


def clean_intermediates() -> None:
    for d in (DIST, WORK):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 Credential Vault 单文件可执行程序到 release/")
    ap.add_argument("--frontend", action="store_true", help="构建前先执行 npm run build")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口（调试）")
    ap.add_argument("--smoke", action="store_true", help="构建后运行 --print-paths 校验（隐含 --console）")
    ap.add_argument("--keep-intermediates", action="store_true", help="保留 dist/ 与 build/ 中间产物")
    args = ap.parse_args()

    console = args.console or args.smoke

    print("=" * 60)
    print(f"目标平台   : {platform.system()} {platform.machine()}")
    print(f"Python     : {sys.version.split()[0]}")
    print(f"产物名     : {EXE_NAME}")
    print(f"控制台模式 : {console}")
    print("=" * 60)

    if args.frontend:
        build_frontend()
    run_pyinstaller(console)
    exe = assemble_release()

    if not args.keep_intermediates:
        clean_intermediates()

    if args.smoke:
        try:
            smoke_test(exe)
        except Exception as e:
            print(f"冒烟校验执行失败（可忽略，多为无显示环境）：{e}", file=sys.stderr)

    # 汇总 release/ 内容
    print("\n" + "=" * 60)
    print(f"构建完成 → {RELEASE}")
    print("=" * 60)
    total = 0
    for p in sorted(RELEASE.rglob("*")):
        if p.is_file():
            total += p.stat().st_size
    print(f"  {EXE_NAME:<20s} {human_size(exe.stat().st_size)}")
    plugins_dir = RELEASE / "plugins"
    if plugins_dir.exists():
        # 两层结构：类别目录（llm/、api/…）下挂服务子插件，根级也可能有平铺插件
        flat, categories = [], []
        for entry in sorted(plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "manifest.json").exists():
                flat.append(entry.name)
            else:
                subs = [d.name for d in sorted(entry.iterdir())
                        if d.is_dir() and (d / "manifest.json").exists()]
                categories.append(f"{entry.name}/（{len(subs)} 个服务）")
        parts = categories + flat
        print(f"  {'plugins/':<20s} 外部可改插件：{', '.join(parts)}")
    print(f"  {'合计':<18s} {human_size(total)}")
    if IS_WINDOWS:
        print("\n运行前提：Windows 需已安装 WebView2 Runtime（Win10/11 通常已内置）。")
    print("data/ 将在首次运行时自动创建于 release/ 下。")


if __name__ == "__main__":
    main()
