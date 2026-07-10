"""Build a document AFP whose text maps through the coded-font chain.

Usage:
    python tools/make_coded_font_sample.py [output_path]

Exercises the classic self-contained AFP font mapping that readAFP resolves
in ``ptoca.extract_pages`` / ``foca.parse_coded_fonts``:

    MCF group  ──FQN X'8E'──▶  coded font  (BCF/CFI)
    coded font ────────────▶  font character set + code page
    char set + code page   ─▶  the file's own glyphs

A resource group carries a real raster character set (lifted from the
public ``testdata/foca_sample.afp``), a hand-built code page mapping bytes
to that char set's GCGIDs, and a coded font whose CFI binds the two. The
page's MCF names *only* the coded font (FQN X'8E'), and by a rotation-
selector name (``X1…``) whose resource is embedded as ``X0…`` — so the
fixture also covers the rotation-insensitive resolution. The PTX selects
that font and emits text the code page maps to real glyphs.

This is the div-free stand-in for a real self-contained embedded-font AFP.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from readafp.foca import BFN, EFN  # noqa: E402
from readafp.parser import iter_fields  # noqa: E402

SOURCE = Path("testdata/foca_sample.afp")
CS_NAME = "C0AAAB00"          # real raster char set in foca_sample (TIMES-ROMAN)
CP_NAME = "T1TSTB00"          # synthetic embedded code-page name
CF_EMBEDDED = "X0TSTB00"      # the coded font as embedded (rotation selector 0)
CF_REFERENCED = "X1TSTB00"    # the same font as the MCF names it (selector 1)

# Bytes the code page maps to GCGIDs present in CS_NAME ('a', 'b', 'c').
TEXT_GLYPHS = [(0x61, "LA010000"), (0x62, "LB010000"), (0x63, "LC010000")]


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + struct.pack(">H", len(body) + 2) + body


def _ebcdic8(text: str) -> bytes:
    return text.encode("cp500")[:8].ljust(8, b"\x40")


def _char_set_bracket() -> bytes:
    """Lift the CS_NAME BFN...EFN span verbatim from the public foca_sample."""
    raw = SOURCE.read_bytes()
    fields = list(iter_fields(raw))
    start = None
    for f in fields:
        if f.sf_id == BFN:
            if _ebcdic8_name(f.data) == CS_NAME:
                start = f.offset
        elif f.sf_id == EFN and start is not None:
            end = f.offset + 9 + len(f.data)  # 0x5A + len(2) + id(3) + flags(1)
            return raw[start:end]              # + seq(2) + data
    raise SystemExit(f"char set {CS_NAME} not found in {SOURCE}")


def _ebcdic8_name(data: bytes) -> str:
    return data[:8].decode("cp500").strip()


def _code_page_bracket() -> bytes:
    """Embedded code page (BCP...ECP) mapping bytes -> the char set's GCGIDs."""
    cpi = bytearray()
    for cp, gcgid in TEXT_GLYPHS:
        # CPI single-byte record: GCGID(8 EBCDIC) + section(1) + code point(1).
        cpi += _ebcdic8(gcgid) + bytes([0x00, cp])
    out = bytearray()
    out += _sf(0xD3A887, _ebcdic8(CP_NAME))   # BCP
    out += _sf(0xD38C87, bytes(cpi))          # CPI
    out += _sf(0xD3A987, _ebcdic8(CP_NAME))   # ECP
    return bytes(out)


def _coded_font_bracket() -> bytes:
    """Coded font (BCF...ECF) whose CFI binds the char set + code page."""
    # CFI (X'D38C8A'): char set name (8) + code page name (8) + reserved.
    cfi = _ebcdic8(CS_NAME) + _ebcdic8(CP_NAME) + b"\x00" * 9
    out = bytearray()
    out += _sf(0xD3A88A, _ebcdic8(CF_EMBEDDED))  # BCF
    out += _sf(0xD38C8A, cfi)                    # CFI
    out += _sf(0xD3A98A, _ebcdic8(CF_EMBEDDED))  # ECF
    return bytes(out)


def _mcf_coded_font() -> bytes:
    """MCF (format 2) mapping local id 1 -> a coded font by name (FQN X'8E')."""
    name = CF_REFERENCED.encode("cp500")
    fqn = bytes([2 + 2 + len(name), 0x02, 0x8E, 0x00]) + name
    rid = bytes([0x04, 0x24, 0x05, 0x01])         # Resource Local ID = 1
    group = struct.pack(">H", len(fqn + rid) + 2) + fqn + rid
    return _sf(0xD3AB8A, group)


def _ptx_run() -> bytes:
    """PTX: position, select font 1, emit the code-page-mapped bytes."""
    esc = b"\x2b\xd3"
    ami = bytes([0x04, 0xC7]) + struct.pack(">H", 1000)   # inline = 1000
    amb = bytes([0x04, 0xD3]) + struct.pack(">H", 2000)   # baseline = 2000
    scfl = bytes([0x03, 0xF1, 0x01])                       # SCFL local id 1
    text = bytes([cp for cp, _g in TEXT_GLYPHS])
    trn = bytes([len(text) + 2, 0xDA]) + text              # TRN (unchained)
    return _sf(0xD3EE9B, esc + ami + amb + scfl + trn)


def _pgd() -> bytes:
    data = (bytes([0, 0]) + struct.pack(">HH", 14400, 14400)
            + (12240).to_bytes(3, "big") + (15840).to_bytes(3, "big"))
    return _sf(0xD3A6AF, data)


def build_afp() -> bytes:
    out = bytearray()
    out += _sf(0xD3A8A8, b"CFDOC\x00\x00\x00")         # BDT
    out += _sf(0xD3A8C6, b"RES\x00\x00\x00\x00\x00")   # BRG
    out += _code_page_bracket()
    out += _char_set_bracket()
    out += _coded_font_bracket()
    out += _sf(0xD3A9C6, b"RES\x00\x00\x00\x00\x00")   # ERG
    out += _sf(0xD3A8AF, _ebcdic8("PAGE0001"))         # BPG
    out += _sf(0xD3A8C9, _ebcdic8("AEG00001"))         # BAG
    out += _pgd()
    out += _mcf_coded_font()
    out += _sf(0xD3A9C9, _ebcdic8("AEG00001"))         # EAG
    out += _sf(0xD3A89B, _ebcdic8("PT000001"))         # BPT
    out += _ptx_run()
    out += _sf(0xD3A99B, _ebcdic8("PT000001"))         # EPT
    out += _sf(0xD3A9AF, _ebcdic8("PAGE0001"))         # EPG
    out += _sf(0xD3A9A8, b"CFDOC\x00\x00\x00")         # EDT
    return bytes(out)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "testdata/coded_font_sample.afp"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_afp()
    out.write_bytes(data)
    print(f"Wrote {len(data):,} bytes -> {out}")
