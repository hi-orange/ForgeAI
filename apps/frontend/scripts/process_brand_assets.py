"""Process Forge brand assets: transparent PNG mark + favicon.ico."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ASSETS = Path(
    r"C:\Users\Administrator\.cursor\projects\f-project-ForgeAI\assets"
)
OUT_DIR = Path(r"f:\project\ForgeAI\apps\frontend\src\assets\brand")
PUBLIC_DIR = Path(r"f:\project\ForgeAI\apps\frontend\public")

ICON_SRC = next(ASSETS.glob("*15_55_06__4___1*.png"))
LOGO_SRC = next(ASSETS.glob("*15_55_05__2_-7a0c82f4*.png"))


def to_transparent(im: Image.Image, threshold: int = 35) -> Image.Image:
    """Treat near-black pixels as transparent."""
    rgba = im.convert("RGBA")
    pixels = rgba.load()
    assert pixels is not None
    w, h = rgba.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if r <= threshold and g <= threshold and b <= threshold:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def trim_alpha(im: Image.Image, pad: int = 8) -> Image.Image:
    bbox = im.getbbox()
    if not bbox:
        return im
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(im.width, right + pad)
    bottom = min(im.height, bottom + pad)
    return im.crop((left, top, right, bottom))


def square_pad(im: Image.Image, fill=(0, 0, 0, 0)) -> Image.Image:
    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), fill)
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    return canvas


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    print("icon src exists:", ICON_SRC.exists(), ICON_SRC)
    print("logo src exists:", LOGO_SRC.exists(), LOGO_SRC)

    icon = trim_alpha(to_transparent(Image.open(ICON_SRC)))
    icon_sq = square_pad(icon)
    mark_path = OUT_DIR / "forge-mark.png"
    icon_sq.resize((256, 256), Image.Resampling.LANCZOS).save(mark_path, optimize=True)
    print("wrote", mark_path, icon_sq.size)

    logo = trim_alpha(to_transparent(Image.open(LOGO_SRC)))
    logo_path = OUT_DIR / "forge-logo.png"
    # Keep reasonable width for UI use
    max_w = 720
    if logo.width > max_w:
        ratio = max_w / logo.width
        logo = logo.resize((max_w, int(logo.height * ratio)), Image.Resampling.LANCZOS)
    logo.save(logo_path, optimize=True)
    print("wrote", logo_path, logo.size)

    # favicon multi-size ico
    favicon_base = icon_sq.resize((256, 256), Image.Resampling.LANCZOS)
    sizes = [(16, 16), (32, 32), (48, 48)]
    ico_images = [favicon_base.resize(s, Image.Resampling.LANCZOS) for s in sizes]
    ico_path = PUBLIC_DIR / "favicon.ico"
    ico_images[0].save(
        ico_path,
        format="ICO",
        sizes=[im.size for im in ico_images],
        append_images=ico_images[1:],
    )
    # Also drop a png favicon for modern browsers
    png_path = PUBLIC_DIR / "favicon.png"
    favicon_base.resize((32, 32), Image.Resampling.LANCZOS).save(png_path, optimize=True)
    print("wrote", ico_path)
    print("wrote", png_path)


if __name__ == "__main__":
    main()
