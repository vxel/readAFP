"""Pixel-level regression for the embedded-glyph recolor filter.

The recolor is an SVG ``<filter>`` whose effect only exists once a real
engine rasterizes it — markup-only assertions (test_ptoca.py) cannot see
it, which is exactly how an inverted filter (flood on the background box
instead of the ink) shipped and passed. This renders the SVG in a headless
browser and samples actual pixels: the ink must take the text color and the
background must stay clear. Skips when Playwright or a browser is absent, so
the core suite still runs everywhere.
"""

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from readafp.foca import _glyph_png
from readafp.ptoca import ImageRef, Page
from readafp.render import page_to_svg

# JS: rasterize the SVG to a canvas and read RGBA at each requested point.
_SAMPLE_JS = """
async ({svg, w, h, points}) => {
  const img = new Image();
  img.width = w; img.height = h;
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  await img.decode();
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0, w, h);
  return points.map(([x, y]) => Array.from(ctx.getImageData(x, y, 1, 1).data));
}
"""


def _launch(p):
    """Launch chromium, falling back to a system Edge/Chrome channel."""
    for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            return p.chromium.launch(**kwargs)
        except Exception:
            continue
    return None


def _sample(page: Page, points):
    """Render ``page`` to SVG and return RGBA tuples at each (x, y) pixel."""
    w, h = page.width, page.height
    svg = page_to_svg(page).replace("<svg ", f'<svg width="{w}" height="{h}" ', 1)
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        if browser is None:
            pytest.skip("no browser available for headless render")
        tab = browser.new_page()
        try:
            rgba = tab.evaluate(_SAMPLE_JS, {"svg": svg, "w": w, "h": h,
                                             "points": points})
        finally:
            browser.close()
    return rgba


def _solid_glyph_page(recolor):
    """A 100x100 white page with one 80x80 all-black glyph bitmap at (10,10)."""
    png = _glyph_png(b"\xff" * 8, 8, 8)  # every pel toned -> solid black block
    page = Page(width=100, height=100, units_per_inch=240)
    page.images.append(ImageRef(x=10, y=10, width=80, height=80,
                                mime="image/png", data=png, crisp=True,
                                recolor=recolor))
    return page


def test_recolored_glyph_ink_takes_the_color() -> None:
    # The solid block is all ink: its center must render as the text color,
    # not white. The inverted filter floods the (absent) background instead,
    # leaving the block transparent -> center would read white. This is the
    # exact pixel that bug would have failed.
    center, corner = _sample(_solid_glyph_page("#ff0000"), [(50, 50), (3, 3)])
    r, g, b, a = center
    assert a > 200, f"ink should be opaque, got alpha {a}"
    assert r > 180 and g < 80 and b < 80, f"ink should be red, got {center}"
    # A corner outside the glyph is bare page: white.
    assert corner[:3] == [255, 255, 255], f"page should be white, got {corner}"


def test_uncolored_glyph_stays_black() -> None:
    # recolor=None leaves the bitmap untouched: the block renders black.
    center, = _sample(_solid_glyph_page(None), [(50, 50)])
    r, g, b, a = center
    assert a > 200 and r < 70 and g < 70 and b < 70, \
        f"uncolored glyph should be black, got {center}"


def test_rotated_glyph_renders_in_rotated_position() -> None:
    # STO 90 is a clockwise rotation about the run origin: a block placed to
    # the RIGHT of the pivot (100,100) must render BELOW it. This is the
    # geometry only a real engine resolves — markup can't confirm direction.
    png = _glyph_png(b"\xff" * 8, 8, 8)  # solid block
    page = Page(width=200, height=200, units_per_inch=240)
    page.images.append(ImageRef(x=110, y=85, width=30, height=30,
                                mime="image/png", data=png, crisp=True,
                                rotate=(90, 100, 100)))
    below, right = _sample(page, [(100, 125), (125, 100)])
    assert below[3] > 200 and below[0] < 70, \
        f"rotated block should land below the origin, got {below}"
    assert right[:3] == [255, 255, 255], \
        f"the un-rotated location should be empty, got {right}"
