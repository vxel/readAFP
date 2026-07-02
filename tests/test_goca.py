"""Tests for GOCA drawing-order decoding and SVG rendering."""

import math
import re
import struct
from pathlib import Path

import pytest

from readafp.goca import (
    GocaGraphic,
    _order_format,
    draw_goca,
    iter_orders,
    parse_gdd,
)
from readafp.parser import iter_fields
from readafp.ptoca import VectorGraphic, extract_pages

TESTDATA = Path(__file__).parent.parent / "testdata"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(sf_id: int, data: bytes = b"") -> bytes:
    """Build a minimal MO:DCA structured field."""
    length = 8 + len(data)
    id3 = bytes([(sf_id >> 16) & 0xFF, (sf_id >> 8) & 0xFF, sf_id & 0xFF])
    return b"\x5A" + struct.pack(">H", length) + id3 + b"\x00\x00\x00" + data


def _begin_segment(drawing_orders: bytes) -> bytes:
    """Wrap drawing orders in a chained Begin Segment command (0x70)."""
    segl = len(drawing_orders)
    name = b"GOCA"  # 4-byte name (ASCII for clarity)
    psname = b"GOCA"
    # NAME(4) + FLAG1(1) + FLAG2(1=0 means chained) + SEGL(2) + P/SNAME(4)
    params = name + b"\x00\x00" + struct.pack(">H", segl) + psname
    assert len(params) == 12
    return bytes([0x70, 0x0C]) + params + drawing_orders


def _gdd_bytes() -> bytes:
    """GDD with 0xF6 Window Specification: GPS 1000×800, 100 units/inch."""
    # 0xF6 parameter: FLAGS(1) RES3(1) CFORMAT(1) UBASE(1) XRESOL(2) YRESOL(2)
    #                 RES2(2) XLWIND(2) XRWIND(2) YBWIND(2) YTWIND(2) = 18 bytes
    params = (
        b"\x00\x00\x00"      # FLAGS + RES3 + CFORMAT
        b"\x00"              # UBASE = 0 (ten inches)
        + struct.pack(">H", 1000)   # XRESOL = 1000 → gps_upi = 100
        + struct.pack(">H", 1000)   # YRESOL
        + b"\x00\x00"               # RES2
        + struct.pack(">h", 0)      # XLWIND
        + struct.pack(">h", 1000)   # XRWIND
        + struct.pack(">h", 0)      # YBWIND
        + struct.pack(">h", 800)    # YTWIND
    )
    assert len(params) == 18
    return bytes([0xF6, len(params)]) + params


def _obd_bytes(width_lu: int = 1440, height_lu: int = 1152) -> bytes:
    """OBD body: 1440 L-units/inch object area of given size."""
    # Triplet 0x4B: TRIPLENGTH(1) + TRIPID(1) + YUNITS(2) + XUNITS(2)
    #               units = 14400 per 10 inches = 1440/inch
    t4b = bytes([0x06, 0x4B]) + struct.pack(">HH", 14400, 14400)
    # Triplet 0x4C: TRIPLENGTH(1) + TRIPID(1) + FLAGS(1) + XSIZE(3) + YSIZE(3)
    t4c = bytes([0x09, 0x4C, 0x00]) + width_lu.to_bytes(3, "big") + height_lu.to_bytes(3, "big")
    return t4b + t4c


def _obp_bytes(x: int = 720, y: int = 720) -> bytes:
    """OBP body: position (x, y) in page L-units."""
    # OAPosID(1) + RGLength(1) + XoaOSet(3) + YoaOSet(3)
    return b"\x00\x06" + x.to_bytes(3, "big", signed=True) + y.to_bytes(3, "big", signed=True)


def _synthetic_afp(gdd: bytes, gad: bytes) -> list:
    """Build a minimal AFP field list containing one GOCA object on page 1."""
    fields_bytes = (
        _sf(0xD3A8A8)      # BDT
        + _sf(0xD3A8AF)    # BPG
        + _sf(0xD3A8BB)    # BGR
        + _sf(0xD3A66B, _obd_bytes())   # OBD
        + _sf(0xD3AC6B, _obp_bytes())   # OBP
        + _sf(0xD3A6BB, gdd)            # GDD
        + _sf(0xD3EEBB, gad)            # GAD
        + _sf(0xD3A9BB)    # EGR
        + _sf(0xD3A9AF)    # EPG
        + _sf(0xD3A9A8)    # EDT
    )
    from readafp.parser import iter_fields
    return list(iter_fields(fields_bytes))


# ---------------------------------------------------------------------------
# Unit tests: _order_format
# ---------------------------------------------------------------------------

def test_order_format_fixed1() -> None:
    assert _order_format(0x00) == "fixed1"


def test_order_format_extended() -> None:
    assert _order_format(0xFE) == "extended"


def test_order_format_fixed2_architecture_note() -> None:
    # 0x71 (End Segment) is fixed 2-byte by architecture note
    assert _order_format(0x71) == "fixed2"


def test_order_format_fixed2_nibble_rule() -> None:
    # upper nibble 0 < 8, lower nibble 8 >= 8
    assert _order_format(0x08) == "fixed2"   # GSPS
    assert _order_format(0x0A) == "fixed2"   # GSCOL
    assert _order_format(0x68) == "fixed2"   # GBAR (upper=6<8, lower=8>=8)


def test_order_format_long() -> None:
    assert _order_format(0x21) == "long"     # GSCP
    assert _order_format(0xC1) == "long"     # GLINE
    assert _order_format(0x81) == "long"     # GCLINE
    assert _order_format(0xC0) == "long"     # GBOX
    assert _order_format(0xB2) == "long"     # GSPCOL


# ---------------------------------------------------------------------------
# Unit tests: iter_orders
# ---------------------------------------------------------------------------

def test_iter_orders_yields_gscp() -> None:
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 100, 400)
    gad = _begin_segment(gscp)
    orders = list(iter_orders(gad))
    assert len(orders) == 1
    code, params = orders[0]
    assert code == 0x21
    assert struct.unpack_from(">h", params, 0)[0] == 100
    assert struct.unpack_from(">h", params, 2)[0] == 400


def test_iter_orders_skips_unchained_segment() -> None:
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 50, 50)
    segl = len(gscp)
    name = b"SKIP"
    # FLAG2 = 0x80 → unchained, should be skipped
    header = bytes([0x70, 0x0C]) + name + bytes([0x00, 0x80]) + struct.pack(">H", segl) + b"SKIP"
    gad = header + gscp
    orders = list(iter_orders(gad))
    assert orders == []


def test_iter_orders_multiple_in_one_segment() -> None:
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 0, 0)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 500, 500)
    gad = _begin_segment(gscp + gcline)
    codes = [c for c, _ in iter_orders(gad)]
    assert codes == [0x21, 0x81]


def test_iter_orders_fixed2_gscol() -> None:
    # GSCOL (0x0A) is fixed 2-byte: code + 1 operand
    gscol = bytes([0x0A, 0x08])  # set color to index 8 (black)
    gad = _begin_segment(gscol)
    orders = list(iter_orders(gad))
    assert len(orders) == 1
    code, params = orders[0]
    assert code == 0x0A
    assert params == bytes([0x08])


def test_iter_orders_extended_format() -> None:
    # 0xFE + qualifier + u16 length + data
    ext_data = b"hello"
    ext_order = bytes([0xFE, 0xDC]) + struct.pack(">H", len(ext_data)) + ext_data
    gad = _begin_segment(ext_order)
    orders = list(iter_orders(gad))
    assert len(orders) == 1
    code, params = orders[0]
    assert code == 0xFEDC
    assert params == ext_data


# ---------------------------------------------------------------------------
# Unit tests: parse_gdd
# ---------------------------------------------------------------------------

def test_parse_gdd_extracts_window() -> None:
    gdd = _gdd_bytes()
    result = parse_gdd(gdd)
    assert result is not None
    gps_upi, xlwind, xrwind, ybwind, ytwind = result
    assert gps_upi == pytest.approx(100.0)
    assert xlwind == 0
    assert xrwind == 1000
    assert ybwind == 0
    assert ytwind == 800


def test_parse_gdd_ten_cm_base() -> None:
    # UBASE=1 (ten centimetres): gps_upi = XRESOL * 0.254
    params = (
        b"\x00\x00\x00"
        b"\x01"                          # UBASE = 1 (ten cm)
        + struct.pack(">H", 400)         # XRESOL = 400 → gps_upi = 400 * 0.254 ≈ 101.6
        + struct.pack(">H", 400)
        + b"\x00\x00"
        + struct.pack(">h", 0)
        + struct.pack(">h", 1000)
        + struct.pack(">h", 0)
        + struct.pack(">h", 800)
    )
    gdd = bytes([0xF6, len(params)]) + params
    result = parse_gdd(gdd)
    assert result is not None
    gps_upi, *_ = result
    assert gps_upi == pytest.approx(400 * 0.254)


def test_parse_gdd_returns_none_without_window_spec() -> None:
    assert parse_gdd(b"") is None
    assert parse_gdd(b"\x00\x00") is None


# ---------------------------------------------------------------------------
# Unit tests: draw_goca
# ---------------------------------------------------------------------------

def test_draw_goca_polyline() -> None:
    """GSCP + GCLINE should produce a <polyline> in GPS-space coordinates."""
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 100, 600)
    # GCLINE from current (100,600) to (400, 300)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 400, 300)
    gad = _begin_segment(gscp + gcline)
    result = draw_goca(_gdd_bytes(), gad)
    assert result is not None
    assert isinstance(result, GocaGraphic)
    assert result.gps_w == 1000
    assert result.gps_h == 800
    # SVG Y is flipped: GPS y=600 → SVG y=800-600=200; GPS y=300 → SVG y=500
    assert "100" in result.svg   # GPS x=100 → SVG x=100 (xlwind=0)
    assert "200" in result.svg   # ytwind-600 = 800-600 = 200
    assert "polyline" in result.svg or "points" in result.svg


def test_draw_goca_box() -> None:
    """GBOX should produce a <rect>."""
    # GBOX (0xC0): long format, params = RESERVED(2) + X0(2) + Y0(2) + X1(2) + Y1(2)
    gbox = bytes([0xC0, 0x0A]) + struct.pack(">hhhhh", 0, 100, 600, 900, 200)
    gad = _begin_segment(gbox)
    result = draw_goca(_gdd_bytes(), gad)
    assert result is not None
    assert "rect" in result.svg


def test_draw_goca_color_change() -> None:
    """GSCOL order should change the stroke color used for subsequent elements."""
    # GSCOL (0x0A) fixed 2-byte, index 0x02 = red (0xFF02)
    gscol = bytes([0x0A, 0x02])
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 0, 400)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 500, 400)
    gad = _begin_segment(gscol + gscp + gcline)
    result = draw_goca(_gdd_bytes(), gad)
    assert result is not None
    assert "#ff0000" in result.svg


def test_draw_goca_process_color_rgb() -> None:
    """GSPCOL with RGB should use that color."""
    # GSPCOL (0xB2): reserved(1) color_space(1) nBits×4 color_value(3)
    gspcol_params = bytes([0xB2, 0x09]) + bytes([
        0x00, 0x01,         # reserved + RGB color space
        0x08, 0x08, 0x08, 0x00,  # 8 bits per R, G, B, A
        0xFF, 0x80, 0x00,   # RGB value (orange-ish)
    ])
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 0, 400)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 500, 400)
    gad = _begin_segment(gspcol_params + gscp + gcline)
    result = draw_goca(_gdd_bytes(), gad)
    assert result is not None
    assert "#ff8000" in result.svg


def test_draw_goca_empty_gad() -> None:
    """Empty GAD stream should yield None (no drawing orders)."""
    result = draw_goca(_gdd_bytes(), b"")
    assert result is None


def test_draw_goca_no_window_spec() -> None:
    """Missing GDD Window Specification should yield None."""
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 0, 0)
    gad = _begin_segment(gscp)
    result = draw_goca(b"", gad)
    assert result is None


def test_draw_goca_filled_area() -> None:
    """GBAR ... GEAR should produce a filled <path>."""
    gbar = bytes([0x68, 0x00])   # GBAR fixed 2-byte, no boundary
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 100, 700)
    # GCLINE to three more corners
    gcline = bytes([0x81, 0x0C]) + struct.pack(">hhhhhh", 900, 700, 900, 100, 100, 100)
    gear = bytes([0x60, 0x00])   # GEAR fixed 2-byte

    gad = _begin_segment(gbar + gscp + gcline + gear)
    result = draw_goca(_gdd_bytes(), gad)
    assert result is not None
    assert "<path" in result.svg


# ---------------------------------------------------------------------------
# Integration test: extract_pages places VectorGraphic on page
# ---------------------------------------------------------------------------

def test_extract_pages_goca_graphic_placed() -> None:
    """A BGR...EGR with GDD + GAD should produce a VectorGraphic on the page."""
    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 100, 400)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 800, 400)
    gad = _begin_segment(gscp + gcline)
    fields = _synthetic_afp(_gdd_bytes(), gad)

    pages = extract_pages(fields)
    assert len(pages) == 1
    page = pages[0]
    assert len(page.graphics) == 1

    vg = page.graphics[0]
    assert isinstance(vg, VectorGraphic)
    # OBP placed at (720, 720) in 1440 L-unit/inch space
    assert vg.x == 720
    assert vg.y == 720
    # OBD says 1440×1152 L-units (1 inch × 0.8 inch) at 1440/inch
    assert vg.width == 1440
    assert vg.height == 1152
    assert vg.graphic.gps_w == 1000
    assert vg.graphic.gps_h == 800
    assert "polyline" in vg.graphic.svg or "points" in vg.graphic.svg


def test_extract_pages_goca_missing_gad_yields_no_graphic() -> None:
    """A BGR...EGR with no drawing orders should not crash and adds no graphic."""
    # GAD has a Begin Segment with no drawing orders → draw_goca returns None
    gad = _begin_segment(b"")
    fields = _synthetic_afp(_gdd_bytes(), gad)
    pages = extract_pages(fields)
    assert len(pages) == 1
    # No graphic because empty GAD produces no SVG output
    assert len(pages[0].graphics) == 0


# ---------------------------------------------------------------------------
# Rendering: VectorGraphic in page_to_svg
# ---------------------------------------------------------------------------

def test_page_to_svg_contains_nested_svg() -> None:
    """page_to_svg should emit a nested <svg> for each vector graphic."""
    from readafp.render import page_to_svg
    from readafp.ptoca import Page

    gscp = bytes([0x21, 0x04]) + struct.pack(">hh", 0, 800)
    gcline = bytes([0x81, 0x04]) + struct.pack(">hh", 1000, 0)
    gad = _begin_segment(gscp + gcline)
    fields = _synthetic_afp(_gdd_bytes(), gad)
    pages = extract_pages(fields)
    assert pages

    svg = page_to_svg(pages[0])
    # The outer SVG must contain a nested <svg> for the GOCA graphic
    assert svg.count("<svg") >= 2
    assert 'viewBox="0 0 1000 800"' in svg


# ---------------------------------------------------------------------------
# Partial-arc sweep direction (ground-truth sample)
# ---------------------------------------------------------------------------

def _arc_endpoints(svg: str):
    """Return (start_angle, end_angle, large, sweep) of a rendered GPARC.

    Angles are degrees of the start/end points about the arc centre, in
    screen (y-down) coordinates.
    """
    m = re.search(
        r"M ([\d.eE+-]+),([\d.eE+-]+) L ([\d.eE+-]+),([\d.eE+-]+) "
        r"A ([\d.eE+-]+),([\d.eE+-]+) 0 (\d) (\d) ([\d.eE+-]+),([\d.eE+-]+)",
        svg,
    )
    assert m, f"no GPARC path in {svg!r}"
    cx, cy, sx, sy, _rx, _ry, large, sweep, ex, ey = (float(v) for v in m.groups())
    a0 = math.degrees(math.atan2(sy - cy, sx - cx))
    a1 = math.degrees(math.atan2(ey - cy, ex - cx))
    return a0, a1, int(large), int(sweep)


def test_partial_arc_sweep_directions() -> None:
    sample = TESTDATA / "goca_arc_sample.afp"
    if not sample.exists():
        pytest.skip("run tools/make_goca_arc_sample.py to generate the sample")
    page = extract_pages(list(iter_fields(sample.read_bytes())))[0]
    # The first six cells are circular arcs (the 7th is a rotated ellipse,
    # covered by test_partial_arc_rotated_ellipse_orientation).
    assert len(page.graphics) == 7
    arcs = [_arc_endpoints(g.graphic.svg) for g in page.graphics[:6]]
    assert len(arcs) == 6

    def close(a, b):
        return abs((a - b + 180) % 360 - 180) < 1.0

    # GOCA sweeps CCW; with the y-flip the SVG sweep flag is 0 (CCW on
    # screen). Endpoints are at screen angle -(GPS angle).
    expected = [
        (0, -90, 0),     # 0/90   E -> up
        (0, 180, 0),     # 0/180  top half
        (0, 90, 1),      # 0/270  large 3/4
        (-90, 180, 0),   # 90/90  up -> W
        (90, 0, 0),      # 270/90 down -> E
        (-45, -135, 0),  # 45/90  NE -> NW
    ]
    for (a0, a1, large, sweep), (e0, e1, elarge) in zip(arcs, expected):
        assert close(a0, e0) and close(a1, e1)
        assert large == elarge
        assert sweep == 0  # CCW on screen, never inverted for circular arcs


def test_partial_arc_rotated_ellipse_orientation() -> None:
    # A 45°-rotated ellipse (major axis 2R along 45°, minor R) must emit
    # the SVG arc's x-axis-rotation, not render axis-aligned.
    R = 200
    phi = math.radians(45)
    a, b = 2 * R, R
    # Spec (GSAP): P=a·cosA, Q=b·cosA, R=-b·sinA, S=a·sinA
    p = round(a * math.cos(phi))
    q = round(b * math.cos(phi))
    r = round(-b * math.sin(phi))
    s = round(a * math.sin(phi))
    cx = cy = 500
    gsap = bytes([0x22, 8]) + struct.pack(">hhhh", p, q, r, s)
    gparc = (
        bytes([0xE3, 18])
        + struct.pack(">hh", cx, cy) + struct.pack(">hh", cx, cy)
        + bytes([1, 0])
        + struct.pack(">I", 0) + struct.pack(">I", 90 * 65536)
    )
    gad = _begin_segment(gsap + gparc)
    page = extract_pages(_synthetic_afp(_gdd_bytes(), gad))[0]
    svg = page.graphics[0].graphic.svg
    m = re.search(r"A ([\d.eE+-]+),([\d.eE+-]+) ([\d.eE+-]+) \d \d", svg)
    assert m, svg
    rx, ry, rot = (float(v) for v in m.groups())
    assert rx == pytest.approx(a, abs=2)   # major semi-axis
    assert ry == pytest.approx(b, abs=2)   # minor semi-axis
    assert rot == pytest.approx(-45, abs=1)  # tilt, not 0
