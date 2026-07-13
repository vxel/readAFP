"""Tests for the legacy IM Image Object (IM/1) decoder.

Synthetic descriptors exercise polarity, celled placement, fill replication
and the X'FFFF' default-extent rule; the real ``div.afp`` fixture (which
carries a "COD" logo plus several 2D bar codes stored as IM images) checks
the whole capture → decode → page-placement path end to end.
"""

import struct
import zlib
from pathlib import Path

from readafp.imimage import decode_im_image, parse_ioc_origin
from readafp.parser import iter_fields
from readafp.ptoca import extract_pages


def _fields(path):
    return list(iter_fields(path.read_bytes()))

TESTDATA = Path(__file__).parent.parent / "testdata"


# --- helpers ----------------------------------------------------------------

def _iid(xsize, ysize, dpi=300, xcsized=0, ycsized=0):
    """Build a minimal 32-byte IM Image Input Descriptor."""
    d = bytearray(32)
    struct.pack_into(">H", d, 14, dpi * 10)   # XUnits
    struct.pack_into(">H", d, 16, dpi * 10)   # YUnits
    struct.pack_into(">H", d, 18, xsize)      # XSize
    struct.pack_into(">H", d, 20, ysize)      # YSize
    struct.pack_into(">H", d, 28, xcsized)    # XCSizeD
    struct.pack_into(">H", d, 30, ycsized)    # YCSizeD
    return bytes(d)


def _icp(xco, yco, xcs, ycs, xfil=0xFFFF, yfil=0xFFFF):
    return struct.pack(">HHHHHH", xco, yco, xcs, ycs, xfil, yfil)


def _png_pixels(png):
    """Decode a 1-bpp grayscale PNG into a list of rows of 0/1 (0 = black)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    width = height = depth = color = None
    idat = b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        tag = png[pos + 4 : pos + 8]
        body = png[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    assert depth == 1 and color == 0
    raw = zlib.decompress(idat)
    row_bytes = (width + 7) // 8
    rows = []
    for r in range(height):
        off = r * (row_bytes + 1)
        assert raw[off] == 0  # filter type 0
        line = raw[off + 1 : off + 1 + row_bytes]
        rows.append([(line[x >> 3] >> (7 - (x & 7))) & 1 for x in range(width)])
    return width, height, rows


# --- synthetic decode tests -------------------------------------------------

def test_simple_image_polarity():
    """A toned pel (bit 1) becomes black (PNG value 0); background white."""
    # 8×1 image, no ICP: one raster byte 0b10100000 → pels 0 and 2 toned.
    im = decode_im_image(_iid(8, 1), [(None, bytes([0b10100000]))])
    assert (im.width, im.height, im.resolution) == (8, 1, 300)
    w, h, rows = _png_pixels(im.png)
    assert (w, h) == (8, 1)
    assert rows[0] == [0, 1, 0, 1, 1, 1, 1, 1]  # 0=black at toned pels


def test_celled_placement():
    """Two cells land side by side at their XCOset offsets."""
    left = bytes([0xFF] * 1 * 4)   # 8×4 all toned
    right = bytes([0x00] * 1 * 4)  # 8×4 all background
    im = decode_im_image(
        _iid(16, 4),
        [(_icp(0, 0, 8, 4), left), (_icp(8, 0, 8, 4), right)],
    )
    w, h, rows = _png_pixels(im.png)
    assert (w, h) == (16, 4)
    for row in rows:
        assert row[:8] == [0] * 8   # left cell: black
        assert row[8:] == [1] * 8   # right cell: white


def test_fill_replication():
    """A cell narrower than its fill rectangle repeats across it."""
    # 2-pel-wide cell "10", fill 8 wide → 1010 1010 across the row.
    im = decode_im_image(
        _iid(8, 1),
        [(_icp(0, 0, 2, 1, xfil=8, yfil=1), bytes([0b10000000]))],
    )
    _, _, rows = _png_pixels(im.png)
    assert rows[0] == [0, 1, 0, 1, 0, 1, 0, 1]


def test_ffff_uses_iid_default_extent():
    """XCSize/YCSize of X'FFFF' fall back to the IID default cell extent."""
    im = decode_im_image(
        _iid(8, 1, xcsized=8, ycsized=1),
        [(_icp(0, 0, 0xFFFF, 0xFFFF), bytes([0b11110000]))],
    )
    _, _, rows = _png_pixels(im.png)
    assert rows[0] == [0, 0, 0, 0, 1, 1, 1, 1]


def test_out_of_bounds_cell_is_clipped():
    """A cell reaching past the image is truncated, not an error."""
    im = decode_im_image(
        _iid(8, 1),
        [(_icp(4, 0, 8, 1), bytes([0xFF]))],  # 8-wide cell at x=4 in 8-wide img
    )
    w, h, rows = _png_pixels(im.png)
    assert (w, h) == (8, 1)
    assert rows[0] == [1, 1, 1, 1, 0, 0, 0, 0]  # only x 4..7 painted black


def test_zero_size_returns_none():
    assert decode_im_image(_iid(0, 0), [(None, b"")]) is None
    assert decode_im_image(b"\x00" * 10, [(None, b"")]) is None  # short IID


def test_parse_ioc_origin():
    # XoaOset(3) YoaOset(3) then rotation/constant/mapping bytes.
    ioc = bytes.fromhex("000334" "0002ff") + b"\x00" * 18
    assert parse_ioc_origin(ioc) == (820, 767)
    assert parse_ioc_origin(b"\x00" * 24) == (0, 0)
    assert parse_ioc_origin(b"") == (0, 0)  # too short, no crash


# --- real fixture -----------------------------------------------------------

def _im_blocks(fields):
    """Yield (name, iid, cells) for every BII...EII object in a file."""
    inb = False
    for f in fields:
        if f.sf_id == 0xD3A87B:
            inb, iid, cells, icp, name = True, None, [], None, f.token_name
        elif inb and f.sf_id == 0xD3A67B:
            iid = f.data
        elif inb and f.sf_id == 0xD3AC7B:
            icp = f.data
        elif inb and f.sf_id == 0xD3EE7B:
            cells.append((icp, f.data))
            icp = None
        elif inb and f.sf_id == 0xD3A97B:
            inb = False
            yield name, iid, cells


def test_div_fixture_decodes_all_im_images():
    fields = _fields(TESTDATA / "div.afp")
    blocks = list(_im_blocks(fields))
    assert len(blocks) == 10
    logo = None
    for name, iid, cells in blocks:
        im = decode_im_image(iid, cells)
        assert im is not None and im.resolution == 300
        assert im.png[:8] == b"\x89PNG\r\n\x1a\n"
        if name == "S1CODXXX":
            logo = im
    # The "COD" logo is a 240×128 bilevel raster with real black content.
    assert logo is not None
    w, h, rows = _png_pixels(logo.png)
    assert (w, h) == (240, 128)
    black = sum(px == 0 for row in rows for px in row)
    assert 200 < black < w * h  # some ink, but mostly white background


def _im_placements(pages):
    """Placed IM-image ImageRefs (crisp PNGs of a known IM size)."""
    sizes = {(240, 128), (120, 120), (136, 132)}
    return [
        im
        for pg in pages
        for im in pg.images
        if im.mime == "image/png"
        and im.crisp
        and struct.unpack(">II", im.data[16:24]) in sizes
    ]


def test_div_fixture_places_im_image_on_page():
    """The IM logo, wrapped in a page segment, is composited via IPS."""
    pages = extract_pages(_fields(TESTDATA / "div.afp"))
    # A 240×128 PNG uniquely identifies the COD logo (bar codes are smaller).
    logos = [
        im for im in _im_placements(pages)
        if struct.unpack(">II", im.data[16:24]) == (240, 128)
    ]
    assert logos, "IM image page segment was never placed on a page"
    # Positioned by its IPS offset (6236, 1758), not stranded at the origin.
    assert all((im.x, im.y) == (6236, 1758) for im in logos)


def test_div_fixture_im_images_are_positioned_not_all_top_left():
    """Inline IM bar codes take their IOC origin, so they are spread out."""
    pages = extract_pages(_fields(TESTDATA / "div.afp"))
    placed = _im_placements(pages)
    assert len(placed) >= 10
    # The regression: every IM image landed at (0, 0). Now each carries a
    # real page position and they occupy several distinct spots.
    at_origin = [im for im in placed if (im.x, im.y) == (0, 0)]
    assert not at_origin, "IM images collapsed to the top-left corner"
    distinct = {(im.x, im.y) for im in placed}
    assert len(distinct) >= 3
    # All within the A4 page bounds (11906 × 16838 L-units).
    assert all(0 < im.x < 11906 and 0 < im.y < 16838 for im in placed)
