#!/usr/bin/env python3
"""Render the owner Rich Menu PNG. Requires Pillow only at design time."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 2500
HEIGHT = 843
ROOT = Path(__file__).resolve().parent


def font_path(bold: bool) -> str:
    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("No supported Arial or DejaVu Sans font found")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(bold), size=size)


def centered(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, size: int, fill: str, bold=False):
    draw.text(xy, text, font=font(size, bold), fill=fill, anchor="mm")


def draw_report(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: str):
    draw.rounded_rectangle((cx - 41, cy - 48, cx + 41, cy + 48), radius=8, outline=color, width=13)
    draw.line((cx - 21, cy - 21, cx + 21, cy - 21), fill=color, width=13)
    draw.line((cx - 21, cy + 2, cx + 21, cy + 2), fill=color, width=13)
    draw.line((cx - 21, cy + 25, cx + 5, cy + 25), fill=color, width=13)
    draw.line((cx - 24, cy - 52, cx - 24, cy - 66, cx + 24, cy - 66, cx + 24, cy - 52), fill=color, width=13)


def draw_money(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: str):
    draw.ellipse((cx - 48, cy - 48, cx + 48, cy + 48), outline=color, width=13)
    draw.line((cx, cy - 34, cx, cy + 34), fill=color, width=10)
    centered(draw, (cx, cy), "$", 61, color, bold=True)


def draw_bell(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: str):
    points = [(cx - 52, cy + 31), (cx - 37, cy + 17), (cx - 33, cy - 19), (cx - 22, cy - 47),
              (cx, cy - 58), (cx + 22, cy - 47), (cx + 33, cy - 19), (cx + 37, cy + 17), (cx + 52, cy + 31)]
    draw.line(points, fill=color, width=13, joint="curve")
    draw.line((cx - 52, cy + 31, cx + 52, cy + 31), fill=color, width=13)
    draw.arc((cx - 19, cy + 24, cx + 19, cy + 62), start=0, end=180, fill=color, width=10)


def draw_sheet(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: str):
    draw.rounded_rectangle((cx - 51, cy - 48, cx + 51, cy + 48), radius=8, outline=color, width=12)
    draw.line((cx - 51, cy - 15, cx + 51, cy - 15), fill=color, width=10)
    draw.line((cx - 17, cy - 15, cx - 17, cy + 48), fill=color, width=10)
    draw.line((cx + 17, cy - 15, cx + 17, cy + 48), fill=color, width=10)
    draw.line((cx - 51, cy + 16, cx + 51, cy + 16), fill=color, width=10)


def arrow(draw: ImageDraw.ImageDraw, cx: int, y: int, color: str):
    draw.line((cx - 34, y, cx + 34, y), fill=color, width=10)
    draw.line((cx + 9, y - 25, cx + 34, y, cx + 9, y + 25), fill=color, width=10, joint="curve")


def render(output: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#F4F7F5")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 154), fill="#16352E")
    draw.text((80, 77), "NotiMate", font=font(58, True), fill="#FFFFFF", anchor="lm")
    draw.text((2420, 77), "ПАНЕЛЬ ВЛАДЕЛЬЦА", font=font(30, True), fill="#BFD4CB", anchor="rm")

    cards = [
        (32, "#DDF1E8", "#18755D", ("ПОДРОБНЫЙ", "ОТЧЁТ"), ("Остатки и", "рекомендации"), draw_report, 48),
        (657, "#FFF0D2", "#B97812", ("ДЕНЬГИ",), ("Выручка и", "расходы"), draw_money, 54),
        (1282, "#FBE3DF", "#C45646", ("НАПОМИНАНИЯ",), ("Важное на", "7 дней"), draw_bell, 45),
        (1907, "#E2E8F7", "#536AA7", ("ТАБЛИЦА",), ("Открыть", "Google Sheets"), draw_sheet, 54),
    ]
    for x, icon_bg, accent, titles, subtitles, icon, title_size in cards:
        cx = x + 280
        draw.rounded_rectangle((x, 184, x + 561, 802), radius=34, fill="#FFFFFF", outline="#D7E1DC", width=4)
        draw.ellipse((cx - 76, 272, cx + 76, 424), fill=icon_bg)
        icon(draw, cx, 348, accent)
        title_y = 539 if len(titles) == 1 else 508
        for index, title in enumerate(titles):
            centered(draw, (cx, title_y + index * 59), title, title_size, "#16352E", bold=True)
        centered(draw, (cx, 649), subtitles[0], 30, "#64756F")
        centered(draw, (cx, 690), subtitles[1], 30, "#64756F")
        arrow(draw, cx, 758, accent)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "owner-rich-menu.png")
    args = parser.parse_args()
    render(args.output)


if __name__ == "__main__":
    main()
