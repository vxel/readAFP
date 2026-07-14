"""Tests for the legacy IM Image Object (IM/1) decoder.

Synthetic descriptors exercise polarity, celled placement, fill replication
and the X'FFFF' default-extent rule; synthetic single-page AFP documents
check the whole capture → decode → page-placement path end to end, both for
an IM image placed inline (positioned by its IOC origin) and one wrapped in a
page segment (positioned by an IPS offset). No confidential fixture is used.
"""

import struct
import zlib

from readafp.imimage import decode_im_image, parse_ioc_origin
from readafp.parser import iter_fields
from readafp.ptoca import extract_pages


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


# --- synthetic end-to-end (capture → decode → placement) --------------------

def _sf(sf_id: int, data: bytes = b"") -> bytes:
    """Build one structured-field record."""
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + (len(body) + 2).to_bytes(2, "big") + body


def _name8(text: str) -> bytes:
    return text.encode("cp500")[:8].ljust(8, b"\x40")


def _pgd(upi: int = 1440, w: int = 8500, h: int = 11000) -> bytes:
    # PGD: flags(2) + XpgUnits/YpgUnits per 10 inch + Xpg/Ypg extents.
    return (bytes([0, 0]) + struct.pack(">HH", upi * 10, upi * 10)
            + w.to_bytes(3, "big") + h.to_bytes(3, "big"))


def _ioc(xo: int, yo: int) -> bytes:
    # IOC object-area origin: XoaOset(3) YoaOset(3) then constant/mapping bytes.
    return xo.to_bytes(3, "big") + yo.to_bytes(3, "big") + b"\x00" * 18


def _im_object(iid: bytes, raster: bytes, ioc: bytes = b"",
               name: str = "") -> bytes:
    """A BII...EII IM/1 object: IID, optional IOC, one simple IRD raster."""
    body = _sf(0xD3A87B, _name8(name)) + _sf(0xD3A67B, iid)
    if ioc:
        body += _sf(0xD3A77B, ioc)
    body += _sf(0xD3EE7B, raster) + _sf(0xD3A97B, _name8(name))
    return body


# A 16×8 all-toned raster (→ all-black PNG) at 300 dpi.
_IID_16x8 = _iid(16, 8, dpi=300)
_RASTER_16x8 = bytes([0xFF] * 2 * 8)  # row_bytes=2, 8 rows


def test_inline_im_image_positioned_by_ioc():
    """An IM image placed directly on a page takes its IOC object-area origin
    (image points), scaled to page units — not stranded at the top-left."""
    doc = (
        _sf(0xD3A8A8, b"\x40" * 8)                       # BDT
        + _sf(0xD3A8AF, b"\x40" * 8)                     # BPG
        + _sf(0xD3A8C9, b"\x40" * 8) + _sf(0xD3A6AF, _pgd(upi=1440))
        + _sf(0xD3A9C9, b"\x40" * 8)                     # EAG
        + _im_object(_IID_16x8, _RASTER_16x8, ioc=_ioc(100, 200))
        + _sf(0xD3A9AF, b"\x40" * 8)                     # EPG
        + _sf(0xD3A9A8, b"\x40" * 8)                     # EDT
    )
    page = extract_pages(list(iter_fields(doc)))[0]
    assert len(page.images) == 1
    im = page.images[0]
    assert im.mime == "image/png" and im.crisp and im.data
    # IOC origin (100, 200) image points at 300 dpi → page L-units at 1440 upi.
    assert (im.x, im.y) == (100 * 1440 // 300, 200 * 1440 // 300)
    assert (im.x, im.y) != (0, 0)


def test_page_segment_im_image_placed_by_ips():
    """An IM image wrapped in a page segment leaves the IOC zero and is placed
    by the page's IPS offset (resource referenced by the enclosing BRS name)."""
    seg = (
        _sf(0xD3A8CE, _name8("IMSEG001"))                # BRS (named resource)
        + _sf(0xD3A85F, b"\x40" * 8)                     # BPS (blank name)
        + _im_object(_IID_16x8, _RASTER_16x8)            # BII...EII, IOC zero
        + _sf(0xD3A95F, b"\x40" * 8)                     # EPS
        + _sf(0xD3A9CE, _name8("IMSEG001"))              # ERS
    )
    ips = _name8("IMSEG001") + (6236).to_bytes(3, "big") + (1758).to_bytes(3, "big")
    doc = (
        _sf(0xD3A8A8, b"\x40" * 8)                       # BDT
        + _sf(0xD3A8C6, b"\x40" * 8) + seg + _sf(0xD3A9C6, b"\x40" * 8)  # BRG
        + _sf(0xD3A8AF, b"\x40" * 8)                     # BPG
        + _sf(0xD3A8C9, b"\x40" * 8) + _sf(0xD3A6AF, _pgd(upi=1440))
        + _sf(0xD3A9C9, b"\x40" * 8)                     # EAG
        + _sf(0xD3AF5F, ips)                             # IPS
        + _sf(0xD3A9AF, b"\x40" * 8)                     # EPG
        + _sf(0xD3A9A8, b"\x40" * 8)                     # EDT
    )
    page = extract_pages(list(iter_fields(doc)))[0]
    assert len(page.images) == 1
    im = page.images[0]
    assert im.mime == "image/png" and im.crisp
    # IOC is zero, so the placement is exactly the IPS offset.
    assert (im.x, im.y) == (6236, 1758)
