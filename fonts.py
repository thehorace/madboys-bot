"""
Fonts for the generated images (lineup pitch, result card).

DejaVu Sans ships inside the repo (assets/fonts, free licence in
LICENSE-DejaVu.txt) so images look the same on Railway as locally —
Railway's containers don't come with desktop fonts installed.
"""

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for candidate in (_DIR / name, name):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()
