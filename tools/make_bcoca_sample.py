"""Generate a small synthetic BCOCA QR bar code AFP for the homepage demo.

Usage:
    python tools/make_bcoca_sample.py [output_path]

The corpus only carries QR bar codes inside 367 KB files (they also embed
a TrueType font), too large to bundle. This builds a minimal one-page AFP
with a single QR Code bar code object (BBC/BDD/BDA) encoding a URL, so the
bundled sample stays under a kilobyte.

Byte offsets match what src/readafp/bcoca.py:parse_barcode reads.
"""

import struct
import sys
from pathlib import Path

QR_URL = "https://github.com/bone1966/readAFP"


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + struct.pack(">H", len(body) + 2) + body


def _bdd() -> bytes:
    """Bar Code Data Descriptor: QR symbology at 1440 units/in, 20-mil module."""
    d = bytearray(18)
    d[0] = 0x00                                # unit base: per 10 inches
    struct.pack_into(">H", d, 2, 14400)        # XUPUB -> 1440 units/inch
    struct.pack_into(">H", d, 4, 14400)        # YUPUB
    struct.pack_into(">H", d, 6, 900)          # X extent
    struct.pack_into(">H", d, 8, 900)          # Y extent
    d[12] = 0x20                               # type: QR Code (X'20')
    d[13] = 0x02                               # modifier
    d[17] = 0x14                               # module width: 20 mils
    return bytes(d)


def _bda(url: str) -> bytes:
    """Bar Code Data: origin, QR special-function params, then the data."""
    d = bytearray(14)  # QR params run bytes 5-13; data starts at byte 14
    # d[0] flags = 0 (draw); d[1:5] X/Y offset = 0 within the object area.
    d[8] = 0x01                                # error correction level M
    return bytes(d) + url.encode("ascii")


def _obp(x: int, y: int) -> bytes:
    return b"\x00\x06" + x.to_bytes(3, "big", signed=True) + y.to_bytes(
        3, "big", signed=True
    )


def _pgd(w: int, h: int, upi: int = 1440) -> bytes:
    xu = upi * 10
    return b"\x00\x00" + struct.pack(">HH", xu, xu) + w.to_bytes(
        3, "big"
    ) + h.to_bytes(3, "big")


def build_afp() -> bytes:
    upi = 1440
    page_w = page_h = upi * 2  # 2 x 2 inch
    buf = bytearray()
    buf += _sf(0xD3A8A8)                              # BDT
    buf += _sf(0xD3A8AF)                              # BPG
    buf += _sf(0xD3A8C9)                              # BAG
    buf += _sf(0xD3A6AF, _pgd(page_w, page_h, upi))  # PGD
    buf += _sf(0xD3A9C9)                              # EAG
    buf += _sf(0xD3A8EB)                              # BBC
    buf += _sf(0xD3AC6B, _obp(upi // 2, upi // 2))   # OBP at 0.5in,0.5in
    buf += _sf(0xD3A6EB, _bdd())                     # BDD
    buf += _sf(0xD3EEEB, _bda(QR_URL))               # BDA
    buf += _sf(0xD3A9EB)                              # EBC
    buf += _sf(0xD3A9AF)                              # EPG
    buf += _sf(0xD3A9A8)                              # EDT
    return bytes(buf)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "testdata/bcoca_sample.afp"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_afp()
    out.write_bytes(data)
    print(f"Wrote {len(data):,} bytes -> {out}")
