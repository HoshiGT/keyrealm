#!/usr/bin/env python3
"""把当前源码打包为 release/credential-vault-src.zip（存在则覆盖）。

打包范围 = git 已追踪文件 + 未被 .gitignore 忽略的新增文件（即当前工作树源码），
因此「改完源码直接跑本脚本」即可，无需先 commit。自动排除被 gitignore 的内容：
.venv / frontend/node_modules / data/（含真实密钥）/ release / dist / build /
frontend/dist / __pycache__ 等；不含符号链接、不含密钥。

纯 Python zipfile 实现，跨平台（Windows 上无需系统 zip 命令）。

用法：
    python scripts/pack_source.py
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
OUT = RELEASE / "credential-vault-src.zip"


def _is_secret(rel: str) -> bool:
    """双保险：即便某敏感文件被误追踪，也绝不打包。"""
    name = Path(rel).name
    if "real_keys" in name or name == "master.key":
        return True
    return name.endswith((".db", ".db-wal", ".db-shm"))


def _source_files() -> list[str]:
    """当前工作树中应打包的文件相对路径（tracked + 未忽略的 untracked）。"""
    # -c 已追踪, -o 未追踪, --exclude-standard 对未追踪应用 .gitignore, -z NUL 分隔
    res = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True,
    )
    if res.returncode != 0:
        sys.exit(f"git ls-files 失败：{res.stderr.decode(errors='replace')}")
    raw = res.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\0") if p]


def main() -> None:
    if not (ROOT / ".git").exists():
        sys.exit("当前目录不是 git 仓库，无法确定源码范围")

    files = _source_files()
    if not files:
        sys.exit("未获取到任何文件，请确认在 git 仓库根目录运行")

    leaked = [f for f in files if _is_secret(f)]
    if leaked:
        sys.exit("检测到疑似敏感文件将被打包，已中止：\n  " + "\n  ".join(leaked))

    # 排除产物自身（release/ 已被 gitignore，理论上不会出现，双保险）
    out_rel = OUT.relative_to(ROOT).as_posix()
    files = [f for f in files if f != out_rel]

    RELEASE.mkdir(parents=True, exist_ok=True)

    missing = []
    written = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            src = ROOT / rel
            if not src.exists():  # git 索引里有但磁盘已删（如已 rm 未 add）
                missing.append(rel)
                continue
            zf.write(src, arcname=rel)
            written += 1

    size = OUT.stat().st_size
    print("=" * 58)
    print(f"源码归档 → {OUT.relative_to(ROOT).as_posix()}")
    print("=" * 58)
    print(f"  文件数 : {written}")
    print(f"  体积   : {size / 1024:.1f} KB")
    if missing:
        print(f"  跳过   : {len(missing)} 个（git 索引有但磁盘缺失）")
    print("  已排除 : .venv / node_modules / data(密钥) / release / dist / build / frontend/dist")
    print("  安全   : 无 real_keys / *.db / master.key ✓")


if __name__ == "__main__":
    main()
