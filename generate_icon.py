"""Generate icon.ico from logo.jpg — the /45 brand mark.

Cropped to square (center crop if non-square), downsampled with LANCZOS
to a 256×256 source, then written as a multi-resolution .ico for every
Windows display size."""

import os
from PIL import Image

SIZES = [(256, 256), (128, 128), (96, 96), (64, 64), (48, 48),
         (40, 40), (32, 32), (24, 24), (20, 20), (16, 16)]
SOURCE = "logo.jpg"


def _square(img):
    if img.width == img.height:
        return img
    s = min(img.width, img.height)
    left = (img.width - s) // 2
    top = (img.height - s) // 2
    return img.crop((left, top, left + s, top + s))


def generate(path="icon.ico"):
    if not os.path.exists(SOURCE):
        raise SystemExit(f"missing source image: {SOURCE}")
    img = Image.open(SOURCE).convert("RGBA")
    img = _square(img)
    source = img.resize((256, 256), Image.LANCZOS)
    source.save(path, format="ICO", sizes=SIZES)
    print(f"wrote {path}")


if __name__ == "__main__":
    generate()
