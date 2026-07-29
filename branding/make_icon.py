"""Generate the EMHASS Companion brand icon.

Draws at high resolution with supersampling (4x) then downsamples with
LANCZOS for clean anti-aliased edges, since the source is exact geometry
(polygons) rather than a rasterized photo -- this avoids needing cairosvg or
another SVG rasteriser, which aren't installed and would need native Cairo
libraries on Windows.

Concept: a house (the home EMHASS optimises) pierced by a lightning bolt (the
energy being scheduled) rendered as a price/cost zigzag rather than a plain
flash, tying the mark to "when is it cheap to use power" rather than a
generic smart-home glyph. Two-tone, no text, reads at 24px in a sidebar.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

# Deliberately not EMHASS's own colours (unknown/unverified) and not Home
# Assistant's blue (#03A9F4, already used in this repo's frontend console
# tag) -- a distinct identity for the companion, not a copy of either.
TEAL = (15, 76, 68, 255)  # house
AMBER = (245, 158, 11, 255)  # bolt / price accent
AMBER_DARK = (194, 120, 3, 255)  # bolt shading for a little depth

SUPERSAMPLE = 4
BASE = 256  # final target; @2x is rendered separately at its own resolution


def house_polygon(size: int) -> list[tuple[float, float]]:
    """A plain gabled house silhouette, apex-up, on a size x size canvas.

    No eave overhang: at icon sizes down to ~24px a step in the roofline
    reads as noise, not detail, so the roof is a clean unbroken pentagon
    edge straight from the apex to each wall.
    """
    s = size
    return [
        (0.50 * s, 0.08 * s),  # roof apex
        (0.88 * s, 0.42 * s),  # roof right -> wall
        (0.88 * s, 0.90 * s),  # body bottom-right
        (0.12 * s, 0.90 * s),  # body bottom-left
        (0.12 * s, 0.42 * s),  # wall -> roof left
    ]


def bolt_polygon(size: int) -> list[tuple[float, float]]:
    """A lightning-bolt / price-zigzag, piercing the house diagonally.

    Kept well inside the house's own silhouette (top above 0.08, bottom
    below 0.90) so it reads as cutting *through* the house rather than
    breaking out of its outline.
    """
    s = size
    pts = [
        (0.58, 0.18),
        (0.36, 0.52),
        (0.49, 0.52),
        (0.41, 0.80),
        (0.68, 0.46),
        (0.53, 0.46),
        (0.62, 0.18),
    ]
    return [(x * s, y * s) for x, y in pts]


def render(size: int) -> Image.Image:
    hi = size * SUPERSAMPLE
    img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.polygon(house_polygon(hi), fill=TEAL)

    # A soft dark offset copy first, then the amber bolt on top, gives the
    # bolt a hairline separation from the teal so it visually "cuts through"
    # instead of just looking like a same-colour cutout.
    bolt = bolt_polygon(hi)
    shadow = [(x + hi * 0.012, y + hi * 0.012) for x, y in bolt]
    draw.polygon(shadow, fill=AMBER_DARK)
    draw.polygon(bolt, fill=AMBER)

    return img.resize((size, size), Image.LANCZOS)


def trim(img: Image.Image) -> Image.Image:
    """Crop to the mark's actual bounding box (brand guidelines: minimal empty space)."""
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def render_logo(height: int) -> Image.Image:
    """A landscape variant: the same mark, trimmed, centred on a wide canvas.

    There is no separate wordmark for this project, so "logo" is the icon's
    own silhouette on brand-guideline-shaped canvas rather than a different
    piece of art -- honest, and avoids embedding text that would be
    illegible at the sizes a logo actually gets shown at.
    """
    mark = trim(render(height))
    width = int(height * 1.6)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = (width - mark.width) // 2
    y = (height - mark.height) // 2
    canvas.alpha_composite(mark, (x, y))
    return canvas


def main() -> None:
    brand_dir = (
        pathlib.Path(__file__).parent.parent / "custom_components" / "emhass_companion" / "brand"
    )
    brand_dir.mkdir(parents=True, exist_ok=True)

    render(256).save(brand_dir / "icon.png")
    render(512).save(brand_dir / "icon@2x.png")
    render_logo(256).save(brand_dir / "logo.png")
    render_logo(512).save(brand_dir / "logo@2x.png")

    for f in sorted(brand_dir.glob("*.png")):
        with Image.open(f) as img:
            print(f.name, img.size, img.mode)


if __name__ == "__main__":
    main()
