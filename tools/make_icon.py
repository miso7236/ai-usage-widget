"""앱 아이콘 만들기 (개발용 도구, Pillow 필요).

    python tools/make_icon.py            # widget/assets/app.ico, extension/icon.png 다시 만들기
    python tools/make_icon.py --sheet 경로.png   # 디자인 후보를 한 장에 모아 보기

위젯의 컴팩트 모드와 같은 이중 링을 어두운 둥근 판 위에 그린다.
작은 크기(24픽셀 이하)에서는 링 하나만 굵게 그려 뭉개지지 않게 한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SCALE = 8  # 크게 그린 뒤 줄여서 가장자리를 부드럽게 한다

TILE = "#202124"
TRACK = "#3c4043"
VARIANTS = {
    # 이름: (바깥 링 색, 바깥 링 채움 비율, 안쪽 링 색, 안쪽 링 채움 비율, 판을 그릴지)
    "blue": ("#8ab4f8", 0.72, "#81c995", 0.45, True),
    "warm": ("#fdd663", 0.72, "#81c995", 0.45, True),
    "bare": ("#8ab4f8", 0.72, "#81c995", 0.45, False),
}
DEFAULT = "blue"


def _ring(draw: ImageDraw.ImageDraw, size: int, radius: float, width: float, color: str, fraction: float, track: str) -> None:
    c = size / 2
    box = (c - radius, c - radius, c + radius, c + radius)
    draw.arc(box, 0, 360, fill=track, width=round(width))
    end = -90 + 360 * fraction
    draw.arc(box, -90, end, fill=color, width=round(width))
    # 선 끝을 둥글게
    import math

    mid = radius - width / 2
    for angle in (-90, end):
        x = c + mid * math.cos(math.radians(angle))
        y = c + mid * math.sin(math.radians(angle))
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)


def draw_icon(size: int, variant: str = DEFAULT) -> Image.Image:
    outer_color, outer_frac, inner_color, inner_frac, tile = VARIANTS[variant]
    big = size * SCALE
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    track = TRACK
    if tile:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.23, fill=TILE)
    else:
        track = "#5f6368"
    small = size <= 24
    if small:
        # 작은 크기: 링 하나만 굵게
        _ring(draw, big, big * (0.36 if tile else 0.46), big * 0.17, outer_color, outer_frac, track)
    else:
        outer_r = big * (0.37 if tile else 0.47)
        width = big * 0.105
        _ring(draw, big, outer_r, width, outer_color, outer_frac, track)
        _ring(draw, big, outer_r - width * 1.55, width, inner_color, inner_frac, track)
    return im.resize((size, size), Image.LANCZOS)


def save_assets(variant: str = DEFAULT) -> None:
    ico = ROOT / "widget" / "assets" / "app.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(size, variant) for size in ICO_SIZES]
    # 크기마다 따로 그린 그림을 그대로 담는다 (Pillow가 큰 그림을 줄여 쓰지 않게)
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    draw_icon(48, variant).save(ROOT / "extension" / "icon.png")
    print("저장:", ico, ROOT / "extension" / "icon.png")


def save_sheet(path: Path) -> None:
    sizes = (128, 48, 32, 24, 16)
    pad = 24
    row_h = 128 + pad * 2
    width = pad + sum(s + pad for s in sizes)
    sheet = Image.new("RGB", (width * 2, row_h * len(VARIANTS)), "#ffffff")
    for row, name in enumerate(VARIANTS):
        for half, bg in enumerate(("#f3f3f3", "#1c1c1c")):  # 밝은 작업표시줄, 어두운 작업표시줄
            ImageDraw.Draw(sheet).rectangle((half * width, row * row_h, (half + 1) * width, (row + 1) * row_h), fill=bg)
            x = half * width + pad
            for size in sizes:
                icon = draw_icon(size, name)
                sheet.paste(icon, (x, row * row_h + (row_h - size) // 2), icon)
                x += size + pad
    sheet.save(path)
    print("저장:", path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default=DEFAULT, choices=sorted(VARIANTS))
    parser.add_argument("--sheet", type=Path, help="후보를 한 장에 모은 그림을 이 경로에 저장")
    args = parser.parse_args()
    if args.sheet:
        save_sheet(args.sheet)
    else:
        save_assets(args.variant)
