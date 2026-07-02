"""Generate a GOCA partial-arc test AFP with known start/sweep angles.

Usage:
    python tools/make_goca_arc_sample.py [output_path]

Each cell draws one GPARC (partial arc) of a fixed-radius circle. The
order's "line start" is set to the arc centre, so a radius line is drawn
to the arc's start point — making the start position visible — and the
arc then sweeps counterclockwise (the GOCA convention) by the given
amount. A PTOCA label states the start and sweep angles. This gives exact
ground truth for verifying the rendered sweep direction.

GOCA angles: degrees CCW from +X, with +Y up (GPS). Expected on screen
(Y-down), CCW maps to: 0°=east(right), 90°=north(up), 180°=west(left),
270°=south(down).
"""

import struct
import sys
from pathlib import Path

UPI = 1440
PAGE_W = 12240
PAGE_H = 15840


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    id3 = bytes([(sf_id >> 16) & 0xFF, (sf_id >> 8) & 0xFF, sf_id & 0xFF])
    return b"\x5A" + struct.pack(">H", 8 + len(data)) + id3 + b"\x00\x00\x00" + data


def _begin_segment(orders: bytes, name: bytes = b"ARC1") -> bytes:
    params = name[:4].ljust(4, b"\x00") + b"\x00\x00" + struct.pack(">H", len(orders)) + b"PSEG"
    return bytes([0x70, 0x0C]) + params + orders


def _gdd(gps: int, upi: int = UPI) -> bytes:
    xres = upi * 10
    params = (
        b"\x00\x00\x00\x00"
        + struct.pack(">HH", xres, xres)
        + b"\x00\x00"
        + struct.pack(">hhhh", 0, gps, 0, gps)
    )
    return bytes([0xF6, len(params)]) + params


def _obd(w: int, h: int, upi: int = UPI) -> bytes:
    yu = upi * 10
    t4b = bytes([0x06, 0x4B]) + struct.pack(">HH", yu, yu)
    t4c = bytes([0x09, 0x4C, 0x00]) + w.to_bytes(3, "big") + h.to_bytes(3, "big")
    return t4b + t4c


def _obp(x: int, y: int) -> bytes:
    return b"\x00\x06" + x.to_bytes(3, "big", signed=True) + y.to_bytes(3, "big", signed=True)


def _pgd(upi: int = UPI) -> bytes:
    xu = upi * 10
    return b"\x00\x00" + struct.pack(">HH", xu, xu) + PAGE_W.to_bytes(3, "big") + PAGE_H.to_bytes(3, "big")


def gspcol_rgb(r: int, g: int, b: int) -> bytes:
    params = bytes([0x00, 0x01, 0x08, 0x08, 0x08, 0x00, r, g, b])
    return bytes([0xB2, len(params)]) + params


def gslw(w: int) -> bytes:
    return bytes([0x19, max(1, min(255, w))])


def gsap(p: int, q: int, r: int, s: int) -> bytes:
    return bytes([0x22, 8]) + struct.pack(">hhhh", p, q, r, s)


def gparc_at(x0, y0, cx, cy, start_deg, sweep_deg, mh=1, mfr=0) -> bytes:
    """GPARC: partial arc at given position (radius line from x0,y0)."""
    params = (
        struct.pack(">hh", x0, y0)
        + struct.pack(">hh", cx, cy)
        + bytes([mh, mfr])
        + struct.pack(">I", int(start_deg * 65536))
        + struct.pack(">I", int(sweep_deg * 65536))
    )
    return bytes([0xE3, len(params)]) + params


# PTOCA label helpers (raw control sequences, cp500, default font).
AMI, AMB, TRN = 0xC6, 0xD2, 0xDA


def _ptx(x, y, text) -> bytes:
    seqs = [(AMI, struct.pack(">H", x)), (AMB, struct.pack(">H", y)),
            (TRN, text.encode("cp500"))]
    out = bytearray(b"\x2b\xd3")
    for i, (t, p) in enumerate(seqs):
        out += bytes([2 + len(p), t | (1 if i < len(seqs) - 1 else 0)]) + p
    return bytes(out)


# (start, sweep, description, matrix) cases. matrix=None draws a circle;
# otherwise (p, q, r, s) is the unit-circle → ellipse transform.
ARCS = [
    (0, 90, "start=0 sweep=90 (E->N, upper-right)", None),
    (0, 180, "start=0 sweep=180 (E->N->W, top half)", None),
    (0, 270, "start=0 sweep=270 (3/4, large arc)", None),
    (90, 90, "start=90 sweep=90 (N->W, upper-left)", None),
    (270, 90, "start=270 sweep=90 (S->E, lower-right)", None),
    (45, 90, "start=45 sweep=90 (NE->NW)", None),
    (0, 270, "rotated ellipse 45deg, 2:1 axes", "ellipse45"),
]


def _arc_object(gps: int, start: float, sweep: float, matrix) -> bytes:
    cx = cy = gps // 2
    radius = int(gps * 0.36)
    if matrix == "ellipse45":
        import math
        phi = math.radians(45)
        a, b = radius, radius // 2  # major : minor = 2 : 1
        # Spec (GSAP): P=a·cosA, Q=b·cosA, R=-b·sinA, S=a·sinA
        p = round(a * math.cos(phi))
        q = round(b * math.cos(phi))
        r = round(-b * math.sin(phi))
        s = round(a * math.sin(phi))
    else:
        p, q, r, s = radius, radius, 0, 0   # circle: P=Q=r, R=S=0
    orders = (
        gspcol_rgb(0x11, 0x55, 0xCC)   # blue
        + gslw(10)
        + gsap(p, q, r, s)
        + gparc_at(cx, cy, cx, cy, start, sweep)
    )
    return _begin_segment(orders)


def build_afp() -> bytes:
    margin = int(UPI * 0.4)
    cell = int(UPI * 3.0)
    cols = 2
    buf = bytearray()
    buf += _sf(0xD3A8A8)                                   # BDT
    buf += _sf(0xD3A8AF)                                   # BPG
    buf += _sf(0xD3A8C9)                                   # BAG
    buf += _sf(0xD3A6AF, _pgd())                           # PGD
    buf += _sf(0xD3A9C9)                                   # EAG

    labels = bytearray()
    for i, (start, sweep, desc, matrix) in enumerate(ARCS):
        col, row = i % cols, i // cols
        ox = margin + col * (cell + margin)
        oy = margin + row * (cell + margin)
        buf += _sf(0xD3A8BB)                               # BGR
        buf += _sf(0xD3A66B, _obd(cell, cell))             # OBD
        buf += _sf(0xD3AC6B, _obp(ox, oy))                 # OBP
        buf += _sf(0xD3A6BB, _gdd(cell))                   # GDD
        buf += _sf(0xD3EEBB, _arc_object(cell, start, sweep, matrix))  # GAD
        buf += _sf(0xD3A9BB)                               # EGR
        labels += _ptx(ox, oy + cell + 220, desc)

    buf += _sf(0xD3A89B)                                   # BPT
    buf += _sf(0xD3EE9B, bytes(labels))                    # PTX (labels)
    buf += _sf(0xD3A99B)                                   # EPT
    buf += _sf(0xD3A9AF)                                   # EPG
    buf += _sf(0xD3A9A8)                                   # EDT
    return bytes(buf)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("testdata/goca_arc_sample.afp")
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_afp()
    out.write_bytes(data)
    print(f"Wrote {len(data):,} bytes -> {out}")
