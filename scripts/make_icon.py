#!/usr/bin/env python3
"""生成 Credential Vault 的应用图标资源（确定性，可重复运行）。

不依赖任何外部图片素材，用 Pillow 直接绘制一枚「保险库门 + 钥匙孔」徽标，
并以 4× 超采样后缩放，保证小尺寸（16px）下依旧清晰锐利。

产物：
  assets/icon.ico        多尺寸 ICO（16/24/32/48/64/128/256），供 PyInstaller、Windows exe 使用
  assets/icon.png        256×256 PNG，供托盘（pystray）等按路径加载
  frontend/public/favicon.png  32×32 PNG，供前端页面 favicon
  frontend/public/favicon.svg  矢量 favicon（现代浏览器优先）

用法：python scripts/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
PUBLIC = ROOT / "frontend" / "public"

# 图标基准画布（超采样倍数），最终按需缩放
BASE = 1024
SS = BASE  # 直接在 1024 上绘制，缩放时再降采样

# 渐变主色（深蓝 → 靛蓝，安全/科技感）
TOP = (59, 130, 246)      # #3B82F6
BOTTOM = (29, 78, 216)    # #1D4ED8


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """生成竖向线性渐变 RGB 图。"""
    grad = Image.new("RGB", (size, size), top)
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        r = round(top[0] + (bottom[0] - top[0]) * t)
        g = round(top[1] + (bottom[1] - top[1]) * t)
        b = round(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return grad


def _badge(size: int) -> Image.Image:
    """绘制 size×size 的 RGBA 徽标。"""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # 圆角方形背景（渐变 + 圆角遮罩）
    grad = _vertical_gradient(s, TOP, BOTTOM)
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    radius = int(s * 0.22)
    md.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    cx, cy = s / 2, s / 2

    # 保险库门：白色半透明圆环
    ring_r = s * 0.34
    ring_w = max(2, int(s * 0.035))
    d.ellipse(
        [cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
        outline=(255, 255, 255, 235),
        width=ring_w,
    )

    # 钥匙孔：上方圆 + 下方梯形柄
    head_r = s * 0.085
    head_cy = cy - s * 0.045
    d.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(255, 255, 255, 255),
    )
    stem_top_y = head_cy + head_r * 0.35
    stem_bot_y = cy + s * 0.17
    top_half = head_r * 0.62
    bot_half = head_r * 1.05
    d.polygon(
        [
            (cx - top_half, stem_top_y),
            (cx + top_half, stem_top_y),
            (cx + bot_half, stem_bot_y),
            (cx - bot_half, stem_bot_y),
        ],
        fill=(255, 255, 255, 255),
    )

    return img


def _svg() -> str:
    """等价的矢量 favicon（圆角渐变方块 + 圆环 + 钥匙孔）。"""
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3B82F6"/>
      <stop offset="1" stop-color="#1D4ED8"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="64" height="64" rx="14" ry="14" fill="url(#g)"/>
  <circle cx="32" cy="32" r="21.8" fill="none" stroke="#ffffff" stroke-opacity="0.92" stroke-width="2.4"/>
  <circle cx="32" cy="29.1" r="5.4" fill="#ffffff"/>
  <polygon points="28.6,31.4 35.4,31.4 37.6,42.9 26.4,42.9" fill="#ffffff"/>
</svg>
"""


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)

    master = _badge(SS)

    # 多尺寸 ICO：从全分辨率母版一次性生成所有帧（PIL 按 sizes 逐个降采样）
    ico_sizes = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]
    ico_path = ASSETS / "icon.ico"
    master.save(ico_path, format="ICO", sizes=ico_sizes)

    # 256 PNG（托盘等按路径加载）
    png_path = ASSETS / "icon.png"
    master.resize((256, 256), Image.LANCZOS).save(png_path, format="PNG")

    # 前端 favicon
    fav_png = PUBLIC / "favicon.png"
    master.resize((32, 32), Image.LANCZOS).save(fav_png, format="PNG")
    fav_svg = PUBLIC / "favicon.svg"
    fav_svg.write_text(_svg(), encoding="utf-8")

    print("已生成图标资源：")
    for p in (ico_path, png_path, fav_png, fav_svg):
        print(f"  {p.relative_to(ROOT).as_posix()}  ({p.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
