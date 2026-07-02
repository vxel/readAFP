"""Generate a synthetic GOCA sample AFP file for visual testing.

Usage:
    python tools/make_goca_sample.py [output_path]

Writes to testdata/goca_sample.afp by default.
The file contains one page with four GOCA graphic objects:
  1. Solid-filled rectangle (blue)
  2. Polyline zigzag (red)
  3. Full ellipse (green fill, black stroke)
  4. Cubic Bézier curve (magenta)
"""

import math
import struct
import sys
from pathlib import Path


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    """Build a minimal MO:DCA structured field."""
    length = 8 + len(data)
    id3 = bytes([(sf_id >> 16) & 0xFF, (sf_id >> 8) & 0xFF, sf_id & 0xFF])
    return b"\x5A" + struct.pack(">H", length) + id3 + b"\x00\x00\x00" + data


def _begin_segment(drawing_orders: bytes, name: bytes = b"SEG1") -> bytes:
    """Wrap drawing orders in a chained Begin Segment command."""
    segl = len(drawing_orders)
    params = name[:4].ljust(4, b"\x00") + b"\x00\x00" + struct.pack(">H", segl) + b"PSEG"
    return bytes([0x70, 0x0C]) + params + drawing_orders


def _gdd(gps_w: int = 2880, gps_h: int = 2880, upi: int = 1440) -> bytes:
    """GDD with 0xF6 Window Spec: square GPS window at given units/inch."""
    xresol = upi * 10  # units per 10 inches
    params = (
        b"\x00\x00\x00"                  # FLAGS + RES3 + CFORMAT
        b"\x00"                          # UBASE (ten inches)
        + struct.pack(">H", xresol)      # XRESOL
        + struct.pack(">H", xresol)      # YRESOL
        + b"\x00\x00"                    # RES2
        + struct.pack(">h", 0)           # XLWIND
        + struct.pack(">h", gps_w)       # XRWIND
        + struct.pack(">h", 0)           # YBWIND
        + struct.pack(">h", gps_h)       # YTWIND
    )
    return bytes([0xF6, len(params)]) + params


def _obd(width: int, height: int, upi: int = 1440) -> bytes:
    """OBD triplets: measurement units + object area size (in L-units)."""
    yunits = upi * 10
    t4b = bytes([0x06, 0x4B]) + struct.pack(">HH", yunits, yunits)
    t4c = bytes([0x09, 0x4C, 0x00]) + width.to_bytes(3, "big") + height.to_bytes(3, "big")
    return t4b + t4c


def _obp(x: int, y: int) -> bytes:
    """OBP: object area position (L-units, signed 3-byte)."""
    return b"\x00\x06" + x.to_bytes(3, "big", signed=True) + y.to_bytes(3, "big", signed=True)


def _pgd(width: int = 12240, height: int = 15840, upi: int = 1440) -> bytes:
    """PGD: page geometry — 8.5×11 inch at 1440 L-units/inch."""
    # PGD format: XpgBase(1) YpgBase(1) XpgUnits(2) YpgUnits(2) XpgSize(3) YpgSize(3)
    xunits = upi * 10
    return (
        b"\x00\x00"
        + struct.pack(">HH", xunits, xunits)
        + width.to_bytes(3, "big")
        + height.to_bytes(3, "big")
    )


# ---------------------------------------------------------------------------
# Drawing-order builders
# ---------------------------------------------------------------------------

def gspcol_rgb(r: int, g: int, b: int) -> bytes:
    """GSPCOL Set Process Color, RGB."""
    params = bytes([0x00, 0x01, 0x08, 0x08, 0x08, 0x00, r, g, b])
    return bytes([0xB2, len(params)]) + params


def gscp(x: int, y: int) -> bytes:
    """GSCP Set Current Position."""
    return bytes([0x21, 0x04]) + struct.pack(">hh", x, y)


def gslw(width: int) -> bytes:
    """GSLW Set Line Width (GPS units, fixed 2-byte: code + operand)."""
    return bytes([0x19, max(1, min(255, width))])


def gscol_index(idx: int) -> bytes:
    """GSCOL Set Color by palette index (fixed 2-byte)."""
    return bytes([0x0A, idx])


# GBAR (begin area) and GEAR (end area)
GBAR = bytes([0x68, 0x40])  # 0x40 = draw boundary
GEAR = bytes([0x60, 0x00])


def gcline(*points) -> bytes:
    """GCLINE: polyline from current position through given GPS (x,y) pairs."""
    data = b"".join(struct.pack(">hh", x, y) for x, y in points)
    return bytes([0x81, len(data)]) + data


def gbox(x0: int, y0: int, x1: int, y1: int) -> bytes:
    """GBOX: axis-aligned box at given GPS coordinates."""
    params = struct.pack(">hhhhh", 0, x0, y0, x1, y1)
    return bytes([0xC0, len(params)]) + params


def gfarc_at(x: int, y: int, mh: int, mfr: int = 0) -> bytes:
    """GFARC: full arc (ellipse) at given GPS position."""
    params = struct.pack(">hh", x, y) + bytes([mh, mfr])
    return bytes([0xC7, len(params)]) + params


def gsap(p: int, q: int, r: int, s: int) -> bytes:
    """GSAP: set arc parameters (the 2×2 linear transform)."""
    params = struct.pack(">hhhh", p, q, r, s)
    return bytes([0x22, len(params)]) + params


def gcbez(*points) -> bytes:
    """GCCBEZ: cubic Bézier from current position; points = (cp1,cp2,end)×N."""
    data = b"".join(struct.pack(">hh", x, y) for x, y in points)
    return bytes([0xA5, len(data)]) + data


# ---------------------------------------------------------------------------
# Four demo GOCA objects
# ---------------------------------------------------------------------------

def _object1_filled_rect(gps: int) -> bytes:
    """Blue filled rectangle occupying 60% of the GPS window."""
    m = int(gps * 0.1)
    x0, y0 = m, int(gps * 0.4)
    x1, y1 = int(gps * 0.9), int(gps * 0.9)
    orders = (
        gspcol_rgb(0x22, 0x55, 0xCC)   # blue fill + stroke
        + gslw(8)
        + GBAR
        + gscp(x0, y0)
        + gcline((x1, y0), (x1, y1), (x0, y1), (x0, y0))
        + GEAR
    )
    return _begin_segment(orders, b"RCT1")


def _object2_zigzag(gps: int) -> bytes:
    """Red polyline zigzag across the GPS window."""
    n = 8
    pts = []
    for i in range(n + 1):
        x = int(gps * i / n)
        y = int(gps * 0.5) + int(gps * 0.3 * (1 if i % 2 == 0 else -1))
        pts.append((x, y))
    start = pts[0]
    rest = pts[1:]
    orders = (
        gspcol_rgb(0xCC, 0x22, 0x22)   # red
        + gslw(12)
        + gscp(*start)
        + gcline(*rest)
    )
    return _begin_segment(orders, b"ZAG1")


def _object3_ellipse(gps: int) -> bytes:
    """Green-filled ellipse (aspect ratio 2:1) centred in the GPS window."""
    cx = gps // 2
    cy = gps // 2
    # Arc transform (spec): P=rx, Q=ry, R=S=0 → axis-aligned ellipse
    rx = int(gps * 0.40)
    ry = int(gps * 0.22)
    orders = (
        gspcol_rgb(0x22, 0xAA, 0x44)   # green fill
        + gslw(6)
        + gsap(rx, ry, 0, 0)
        + gfarc_at(cx, cy, 1, 0)       # multiplier = 1.0
    )
    return _begin_segment(orders, b"ELP1")


def _object4_bezier(gps: int) -> bytes:
    """Magenta S-curve cubic Bézier."""
    g = gps
    # Start at bottom-left, sweep to top-right with two control points
    sx, sy = int(g * 0.05), int(g * 0.20)
    cp1x, cp1y = int(g * 0.90), int(g * 0.20)
    cp2x, cp2y = int(g * 0.10), int(g * 0.80)
    ex, ey = int(g * 0.95), int(g * 0.80)
    orders = (
        gspcol_rgb(0xAA, 0x22, 0x99)   # magenta
        + gslw(14)
        + gscp(sx, sy)
        + gcbez((cp1x, cp1y), (cp2x, cp2y), (ex, ey))
    )
    return _begin_segment(orders, b"BEZ1")


# ---------------------------------------------------------------------------
# Layout: four quadrants on one page
# ---------------------------------------------------------------------------

def build_afp() -> bytes:
    upi = 1440
    page_w = 12240  # 8.5 inch
    page_h = 15840  # 11 inch
    margin = int(upi * 0.5)  # 0.5 inch margin

    # Each GOCA object gets a 3.5×3.5 inch cell
    cell = int(upi * 3.5)
    gps = cell  # GPS window matches cell size

    # Cell origins (top-left of each quadrant): x, y in L-units
    positions = [
        (margin,          margin),           # top-left
        (margin + cell + margin, margin),    # top-right
        (margin,          margin + cell + margin),  # bottom-left
        (margin + cell + margin, margin + cell + margin),  # bottom-right
    ]

    makers = [_object1_filled_rect, _object2_zigzag, _object3_ellipse, _object4_bezier]

    buf = bytearray()
    buf += _sf(0xD3A8A8)                  # BDT
    buf += _sf(0xD3A8AF)                  # BPG
    buf += _sf(0xD3A8C9)                  # BAG
    buf += _sf(0xD3A6AF, _pgd(page_w, page_h, upi))  # PGD
    buf += _sf(0xD3A9C9)                  # EAG

    for i, (ox, oy) in enumerate(positions):
        gad_bytes = makers[i](gps)
        buf += _sf(0xD3A8BB)                           # BGR
        buf += _sf(0xD3A66B, _obd(cell, cell, upi))   # OBD
        buf += _sf(0xD3AC6B, _obp(ox, oy))            # OBP
        buf += _sf(0xD3A6BB, _gdd(gps, gps, upi))     # GDD
        buf += _sf(0xD3EEBB, gad_bytes)                # GAD
        buf += _sf(0xD3A9BB)                           # EGR

    buf += _sf(0xD3A9AF)                  # EPG
    buf += _sf(0xD3A9A8)                  # EDT
    return bytes(buf)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("testdata/goca_sample.afp")
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_afp()
    out.write_bytes(data)
    print(f"Written {len(data):,} bytes -> {out}")
