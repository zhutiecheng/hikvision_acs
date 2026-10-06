#!/usr/bin/env python3
"""
生成集成所需的品牌图标。

HACS 校验要求集成自带 custom_components/<domain>/brand/icon.png，
否则会回退去查 Home Assistant 官方 brands 仓库——我们的集成不在那里，
校验就会失败。

**这是占位图标，不是你自己的品牌。** 要换成自己的 logo，直接替换
custom_components/hikvision_acs/brand/icon.png 即可（建议 256 或 512 见方 PNG）。

不依赖 Pillow：自己写 PNG（zlib + struct 都是标准库），并用 2 倍超采样做抗锯齿。

用法：
    python3 tools/make_icon.py
    python3 tools/make_icon.py --color "#1F6FEB" --out /tmp/x.png
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "custom_components" / "hikvision_acs" / "brand" / "icon.png"

SS = 3  # 超采样倍数，用来做抗锯齿


def parse_color(text: str) -> tuple[int, int, int]:
    text = text.lstrip("#")
    if len(text) != 6:
        raise ValueError("颜色要写成 #RRGGBB")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def render(size: int, color: tuple[int, int, int]) -> bytes:
    """画一个圆角方块 + 白色门扇 + 门把手。返回 RGBA 像素。

    设计约束：HACS 里这个图标常以 48px 左右显示，所以形状必须极简、
    对比必须强。门不能画得太大——占满背景时会糊成一块白斑，认不出是门。
    """
    n = size * SS
    bg = color
    px = bytearray(n * n * 4)

    margin = 0.05 * n
    radius = 0.24 * n
    left, top = margin, margin
    right, bottom = n - margin, n - margin

    # 门的几何参数（相对整幅）：收窄、压在中间偏下，留出足够的红边
    door_w, door_h = 0.36 * n, 0.50 * n
    door_l = (n - door_w) / 2
    door_t = (n - door_h) / 2 + 0.035 * n
    door_r = door_l + door_w
    door_b = door_t + door_h
    door_corner = 0.055 * n           # 门顶两角圆角
    knob_r = 0.032 * n
    knob_cx = door_r - 0.075 * n
    knob_cy = door_t + door_h * 0.46

    def in_bg(x: float, y: float) -> bool:
        """圆角方块。四角按象限分别判定，不用共享条件——那样容易写错象限。"""
        if not (left <= x <= right and top <= y <= bottom):
            return False
        if x < left + radius and y < top + radius:
            return (x - (left + radius)) ** 2 + (y - (top + radius)) ** 2 <= radius ** 2
        if x > right - radius and y < top + radius:
            return (x - (right - radius)) ** 2 + (y - (top + radius)) ** 2 <= radius ** 2
        if x < left + radius and y > bottom - radius:
            return (x - (left + radius)) ** 2 + (y - (bottom - radius)) ** 2 <= radius ** 2
        if x > right - radius and y > bottom - radius:
            return (x - (right - radius)) ** 2 + (y - (bottom - radius)) ** 2 <= radius ** 2
        return True

    def in_door(x: float, y: float) -> bool:
        if not (door_l <= x <= door_r and door_t <= y <= door_b):
            return False
        if y < door_t + door_corner:
            if x < door_l + door_corner:
                return ((x - (door_l + door_corner)) ** 2
                        + (y - (door_t + door_corner)) ** 2) <= door_corner ** 2
            if x > door_r - door_corner:
                return ((x - (door_r - door_corner)) ** 2
                        + (y - (door_t + door_corner)) ** 2) <= door_corner ** 2
        return True

    for yy in range(n):
        for xx in range(n):
            x, y = xx + 0.5, yy + 0.5
            if not in_bg(x, y):
                continue

            r, g, b = bg
            if in_door(x, y):
                r = g = b = 255
                if (x - knob_cx) ** 2 + (y - knob_cy) ** 2 <= knob_r ** 2:
                    r, g, b = bg      # 门把手：挖回背景色

            i = (yy * n + xx) * 4
            px[i:i + 4] = bytes((r, g, b, 255))

    # 超采样降采样
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(SS):
                for dx in range(SS):
                    i = ((y * SS + dy) * n + (x * SS + dx)) * 4
                    for c in range(4):
                        acc[c] += px[i + c]
            j = (y * size + x) * 4
            for c in range(4):
                out[j + c] = acc[c] // (SS * SS)
    return bytes(out)


def write_png(path: pathlib.Path, size: int, rgba: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(size):
        raw.append(0)                      # 每行的 filter byte
        raw += rgba[y * size * 4:(y + 1) * size * 4]

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成品牌图标")
    ap.add_argument("--size", type=int, default=512, help="边长像素，默认 512")
    ap.add_argument("--color", default="#C8102E", help="背景色 #RRGGBB，默认海康红")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    color = parse_color(args.color)
    rgba = render(args.size, color)
    out = pathlib.Path(args.out)
    write_png(out, args.size, rgba)
    print(f"✅ 已生成 {out}  ({args.size}x{args.size}, {out.stat().st_size} 字节)")
    print("   这是占位图标，换成你自己的 logo 直接替换该文件即可")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
