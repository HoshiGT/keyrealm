"""校验已构建的可执行程序里是否真的嵌入了 assets/icon.ico。

为什么需要它
------------
「资源管理器显示默认图标」有两大类原因，现象一模一样但修法完全不同：

  A. 图标压根没进 exe（spec 里 icon 为空 / 路径不存在 / 构建用了旧产物）
     → 必须重新构建。
  B. 图标进 exe 了，但 Windows 图标缓存还拿着旧记录
     → 重建无用，要刷缓存（ie4uinit.exe -show，或把 exe 换个目录再看）。

分不清这两者就会在「反复重建但图标依旧」和「明明重建了却去刷缓存」之间来回折腾。
PyInstaller 把 .ico 的**每个条目原样**塞进 exe 的 RT_ICON 资源，所以在 exe 里
按字节搜各条目载荷即可证明「嵌没嵌进去」——256×256 的 PNG 帧近 20 KB，
重复概率可忽略，命中即确证。

用法
----
    python scripts/check_exe_icon.py                       # 默认查 release/ 下的产物
    python scripts/check_exe_icon.py path/to/app.exe

退出码：0 = 全部条目都已嵌入；1 = 未嵌入或条目缺失；2 = 文件/参数问题。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICO = ROOT / "assets" / "icon.ico"

# 自动探测：Windows 下是 .exe，Linux/macOS 构建产物无扩展名
_CANDIDATES = [
    ROOT / "release" / "CredentialVault.exe",
    ROOT / "release" / "CredentialVault",
]


def read_ico_entries(path: Path) -> list[dict]:
    """解析 ICO，返回每个条目的尺寸与载荷。"""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if (reserved, kind) != (0, 1):
        raise ValueError(f"{path} 不是合法的 ICO（reserved={reserved}, type={kind}）")

    entries: list[dict] = []
    off = 6
    for _ in range(count):
        w, h, _colors, _res, _planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", data[off:off + 16]
        )
        off += 16
        payload = data[offset:offset + size]
        entries.append({
            "size": w or 256,
            "bpp": bpp,
            "bytes": size,
            "payload": payload,
            "format": "PNG" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "BMP",
        })
    return entries


def check(exe: Path) -> int:
    if not ICO.exists():
        print(f"✗ 找不到 {ICO}，先运行 python scripts/make_icon.py", file=sys.stderr)
        return 2
    if not exe.exists():
        print(f"✗ 找不到可执行文件：{exe}", file=sys.stderr)
        print("  先在 Windows 上运行 python scripts/build.py --frontend", file=sys.stderr)
        return 2

    entries = read_ico_entries(ICO)
    blob = exe.read_bytes()

    print(f"图标文件：{ICO}（{len(entries)} 帧，{ICO.stat().st_size} 字节）")
    print(f"可执行文件：{exe}（{exe.stat().st_size} 字节）")
    print()

    missing: list[int] = []
    for e in entries:
        hit = e["payload"] in blob
        mark = "✓" if hit else "✗"
        note = "" if hit else "  ← 未找到，该帧没进 exe"
        print(f"  {mark} {e['size']:>3}x{e['size']:<3} {e['format']:<3} "
              f"{e['bytes']:>6} 字节{note}")
        if not hit:
            missing.append(e["size"])

    print()
    if not missing:
        print("结论：图标已嵌入 exe。资源管理器仍显示默认图标 → 是 Windows 图标缓存问题，")
        print("      重建没用，执行下面任一操作后重开资源管理器：")
        print("        ie4uinit.exe -show")
        print("      或把 exe 复制到另一个目录（新路径不带缓存）后查看。")
        return 0

    print(f"结论：有 {len(missing)} 帧未嵌入（{missing}）→ 这次构建没带上图标。")
    print("      逐项排查：")
    print("        1) 构建日志里是否为「[spec] 警告：未找到 assets/icon.ico」；")
    print("        2) 是否在项目根目录执行构建（spec 按 SPECPATH 解析 assets/）；")
    print("        3) exe 的修改时间是否晚于 assets/icon.ico（否则看的是旧产物）。")
    return 1


def main() -> int:
    args = sys.argv[1:]
    if args:
        return check(Path(args[0]).expanduser().resolve())
    for cand in _CANDIDATES:
        if cand.exists():
            return check(cand)
    print("✗ release/ 下没有找到构建产物", file=sys.stderr)
    print("  用法：python scripts/check_exe_icon.py <可执行文件路径>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
