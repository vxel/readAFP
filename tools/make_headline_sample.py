"""Generate a centered-headline AFP + matching PDF ground-truth pair.

Usage:
    python tools/make_headline_sample.py [output_dir]

Builds a one-page document with THREE blue headlines stacked vertically,
plus a PDF that renders the same three lines. Purpose: a minimal,
isolated test case for comparing (up/down) how readAFP centers a headline
in each font.

Each headline's inline (x) position is computed by measuring the string
with the standard Helvetica AFM width table. Because Arial was designed
to be metric-compatible with Helvetica, an x computed from Helvetica
widths renders centered when the browser substitutes Arial -- which is
why readAFP can center the headline as Arial even though it has no
Helvetica font program. The MDR declares the point size so the renderer
uses an exact size instead of estimating it from geometry, keeping the
centering deterministic.

The first two lines (Helvetica, Arial) are therefore perfectly centered.
The THIRD line is a deliberate *contrast*: it is declared Times -- a
serif face that is NOT metric-compatible with Helvetica -- yet its x is
still computed from Helvetica widths (the naive author bug). Times glyphs
are narrower, so the real text is shorter than the slot the x reserves
and the line sits visibly left of center. The PDF draws this line in real
Times-Roman at the same x, so the PDF and readAFP's SVG agree (both
off-center) and the printed margin table exposes the offset -- i.e. this
is what "off-center" looks like, faithfully reproduced.
"""

import struct
import sys
from pathlib import Path

UPI = 1440
PT = UPI // 72  # 20 L-units per point at 1440/inch
PAGE_W = int(8.5 * UPI)   # 12240
PAGE_H = int(11 * UPI)    # 15840

HEADLINE_PT = 24
# Three blue headlines, stacked. (text, MDR family, baseline-y in L-units).
# Helvetica + Arial center perfectly (Arial is metric-compatible). Times is
# the contrast line: declared serif but placed with Helvetica widths, so it
# renders off-center -- see FAMILY_METRIC and the margin report below.
HEADLINES = [
    ("Quarterly Benefits Summary (Helvetica)", "Helvetica", int(2.0 * UPI)),
    ("Quarterly Benefits Summary (Arial)", "Arial", int(2.7 * UPI)),
    ("Quarterly Benefits Summary (Times)", "Times", int(3.4 * UPI)),
]

# PTOCA control-sequence function types (unchained/even values).
AMI, AMB, TRN, STC, SCFL = 0xC6, 0xD2, 0xDA, 0x74, 0xF0
BLUE, BLACK = 0x0001, 0x0008

# Standard Helvetica AFM advance widths (units / 1000 em) for ASCII 32-126.
# Arial is metric-compatible, so these center an Arial substitute too.
_HELV_W = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
    278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
    584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
    500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
    278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]

# Standard Times-Roman AFM advance widths (units / 1000 em) for ASCII 32-126.
# Times is NOT metric-compatible with Helvetica -- it is narrower -- so a
# Helvetica-computed x leaves the real Times text sitting left of center.
_TIMES_W = [
    250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333,
    250, 278, 500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 278, 278,
    564, 564, 564, 444, 921, 722, 667, 667, 722, 611, 556, 722, 722, 333,
    389, 722, 611, 889, 722, 722, 556, 722, 667, 556, 611, 722, 722, 944,
    722, 722, 611, 333, 278, 333, 469, 500, 333, 444, 500, 444, 500, 444,
    333, 500, 500, 278, 278, 500, 278, 778, 500, 500, 500, 500, 333, 389,
    278, 500, 500, 722, 500, 500, 444, 480, 200, 480, 541,
]

# Which AFM table gives each declared family its *true* rendered width.
FAMILY_METRIC = {"Helvetica": _HELV_W, "Arial": _HELV_W, "Times": _TIMES_W}


def _width_em(text: str, table=_HELV_W) -> int:
    """String advance width in 1000-em units using the given AFM table."""
    total = 0
    for ch in text:
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else 556
    return total


def _helv_width_em(text: str) -> int:
    """String advance width in 1000-em units using Helvetica metrics."""
    return _width_em(text, _HELV_W)


def _centered_x_lunits(text: str, point_size: int, page_w: int) -> int:
    """Inline (x) L-unit position that centers ``text`` on the page."""
    width = _helv_width_em(text) / 1000 * point_size * PT
    return int((page_w - width) / 2)


# ---------------------------------------------------------------------------
# AFP
# ---------------------------------------------------------------------------
def _sf(sf_id: int, data: bytes = b"") -> bytes:
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + struct.pack(">H", len(body) + 2) + body


def _name(s: str) -> bytes:
    return s.encode("cp500")[:8].ljust(8, b"\x40")


def _ptx(*seqs) -> bytes:
    out = bytearray(b"\x2b\xd3")
    for i, (t, p) in enumerate(seqs):
        chained = 1 if i < len(seqs) - 1 else 0
        out += bytes([2 + len(p), t | chained]) + p
    return bytes(out)


def _move(x: int, y: int):
    return (AMI, struct.pack(">H", x)), (AMB, struct.pack(">H", y))


def _pgd() -> bytes:
    xu = UPI * 10
    return (
        b"\x00\x00"
        + struct.pack(">HH", xu, xu)
        + PAGE_W.to_bytes(3, "big")
        + PAGE_H.to_bytes(3, "big")
    )


def _mdr_triplet_fqn_name(name: str) -> bytes:
    body = b"\xde\x00" + name.encode("cp500")
    return bytes([len(body) + 2, 0x02]) + body


def _mdr_triplet_size(point_size: int) -> bytes:
    # 0x8B descriptor: point size in 1/20 pt at offset 2 (= L-units at 1440).
    return bytes([6, 0x8B, 0x00, 0x00]) + struct.pack(">H", point_size * 20)


def _mdr_triplet_local_id(local_id: int) -> bytes:
    # 0x02 FQN type 0xBE: the local id PTX SCFL selects (last data byte).
    return bytes([5, 0x02, 0xBE, 0x00, local_id])


def _mdr() -> bytes:
    """Map one local id per headline -> its declared font family + size."""
    groups = b""
    for local_id, (_text, family, _y) in enumerate(HEADLINES, start=1):
        triplets = (
            _mdr_triplet_fqn_name(family)
            + _mdr_triplet_size(HEADLINE_PT)
            + _mdr_triplet_local_id(local_id)
        )
        groups += struct.pack(">H", len(triplets) + 2) + triplets
    return groups


def build_afp() -> bytes:
    seqs = []
    for local_id, (text, _family, y) in enumerate(HEADLINES, start=1):
        x = _centered_x_lunits(text, HEADLINE_PT, PAGE_W)
        seqs += [
            (SCFL, bytes([local_id])), (STC, struct.pack(">H", BLUE)),
            *_move(x, y), (TRN, text.encode("cp500")),
        ]
    body = _ptx(*seqs)
    return (
        _sf(0xD3A8A8, _name("HEADTEST"))    # BDT
        + _sf(0xD3A8AF)                     # BPG
        + _sf(0xD3A8C9)                     # BAG
        + _sf(0xD3A6AF, _pgd())             # PGD
        + _sf(0xD3ABC3, _mdr())             # MDR (font map + sizes)
        + _sf(0xD3A9C9)                     # EAG
        + _sf(0xD3A89B)                     # BPT
        + _sf(0xD3EE9B, body)               # PTX
        + _sf(0xD3A99B)                     # EPT
        + _sf(0xD3A9AF)                     # EPG
        + _sf(0xD3A9A8, _name("HEADTEST"))  # EDT
    )


# ---------------------------------------------------------------------------
# PDF (hand-rolled, base-14 Helvetica + Courier, centered)
# ---------------------------------------------------------------------------
def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# The PDF resource each declared family draws with. Helvetica/Arial are
# metric-identical, so both use base-14 Helvetica; the Times contrast line
# uses real base-14 Times-Roman -- so the PDF reproduces the same off-center
# placement readAFP shows (same Helvetica-computed x).
_PDF_FONT_RES = {"Helvetica": "F1", "Arial": "F1", "Times": "F2"}


def build_pdf() -> bytes:
    pw, ph = 612, 792  # 8.5 x 11 in points
    # Every line's x is computed from Helvetica widths (mirroring the AFP),
    # then each is drawn in its declared base-14 font. The centered position
    # is what we validate: Helvetica/Arial land centered, Times off-center.
    lines = []
    for text, family, y in HEADLINES:
        x = (pw - _helv_width_em(text) / 1000 * HEADLINE_PT) / 2
        py = ph - y / PT  # AFP top-origin -> PDF bottom-origin
        res = _PDF_FONT_RES[family]
        lines.append(
            f"BT 0 0 1 rg /{res} {HEADLINE_PT} Tf 1 0 0 1 {x:.2f} {py:.2f} Tm "
            f"({_pdf_escape(text)}) Tj ET\n"
        )
    content = "".join(lines).encode("latin-1")

    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {pw} {ph}]"
            f"/Resources<</Font<</F1 5 0 R/F2 6 0 R>>>>/Contents 4 0 R>>"
        ).encode("latin-1"),
        b"<</Length " + str(len(content)).encode() + b">>\nstream\n"
        + content + b"endstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Times-Roman>>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


if __name__ == "__main__":
    out_dir = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path("testdata/headline_sample")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    afp = build_afp()
    pdf = build_pdf()
    (out_dir / "headline.afp").write_bytes(afp)
    (out_dir / "headline.pdf").write_bytes(pdf)

    # Margin report: each line's x comes from Helvetica widths, but the real
    # text width uses the declared family's own metrics. Equal left/right
    # margins == centered; unequal == the deliberate Times miscentering.
    print(f"  page width {PAGE_W} L-units, center {PAGE_W // 2}")
    print(f"  {'family':<10} {'x(left)':>8} {'width':>7} "
          f"{'right':>8} {'offset':>8}  centered?")
    for text, family, _y in HEADLINES:
        x = _centered_x_lunits(text, HEADLINE_PT, PAGE_W)
        true_w = int(_width_em(text, FAMILY_METRIC[family]) / 1000
                     * HEADLINE_PT * PT)
        right = PAGE_W - x - true_w
        offset = (right - x) // 2  # +ve => text sits left of center
        mark = "yes" if abs(offset) < PT else f"NO (left by {offset})"
        print(f"  {family:<10} {x:>8} {true_w:>7} {right:>8} "
              f"{offset:>8}  {mark}")
    print(f"Wrote {len(afp):,} bytes -> {out_dir / 'headline.afp'}")
    print(f"Wrote {len(pdf):,} bytes -> {out_dir / 'headline.pdf'}")
