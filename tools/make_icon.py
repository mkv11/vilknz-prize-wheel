"""Рисует иконку приложения (колесо в цветах интерфейса) в assets/icon.ico и assets/icon.png.

Запуск: python tools/make_icon.py  (нужен Pillow).
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

COLORS = ["#7c5cff", "#22d3ee", "#f472b6", "#facc15", "#34d399", "#fb923c",
          "#60a5fa", "#e879f9", "#a3e635", "#f87171", "#2dd4bf", "#c084fc"]
OUT = Path(__file__).resolve().parent.parent / "assets"


def draw(size=1024):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = size / 2, size * 0.44
    d.ellipse([c - r - size * 0.03, c - r - size * 0.03, c + r + size * 0.03, c + r + size * 0.03], fill="#171a23")
    step = 360 / len(COLORS)
    for i, color in enumerate(COLORS):
        d.pieslice([c - r, c - r, c + r, c + r], i * step - 90, (i + 1) * step - 90, fill=color)
    hub = size * 0.13
    d.ellipse([c - hub, c - hub, c + hub, c + hub], fill="#ffffff")
    d.ellipse([c - hub * 0.62, c - hub * 0.62, c + hub * 0.62, c + hub * 0.62], fill="#171a23")
    # стрелка справа, как в интерфейсе
    tip = c + r * 0.82
    d.polygon([(tip, c), (size * 0.995, c - size * 0.075), (size * 0.995, c + size * 0.075)],
              fill="#ffffff", outline="#171a23", width=max(2, size // 128))
    return img


def main():
    OUT.mkdir(exist_ok=True)
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("saved", OUT / "icon.ico")


if __name__ == "__main__":
    main()
