"""BCOCA bar code object decoding.

A bar code object (BBC...EBC) carries a Bar Code Data Descriptor (BDD)
holding the Bar Code Symbol Descriptor — units, presentation-space
extents, symbology type/modifier, module width — and Bar Code Data
(BDA) holding flags, the symbol origin, symbology-specific parameters
and the data characters.

Only QR Code (type X'20') is generated — with segno (pure-Python QR
encoder), packed as a bilevel PNG. Other symbologies, including the
corpus's Data Matrix objects (type X'1C', long mistaken for QR here),
are skipped with a log message rather than drawn as the wrong symbol.

Reference: BCOCA Reference, AFPC-0005-11 (docs/specs/bcoca-reference-11.pdf).
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import segno

from readafp.ioca import pack_png

logger = logging.getLogger(__name__)

# BSD type codes (BCOCA Table 9): X'1C' is Data Matrix, NOT QR.
TYPE_DATA_MATRIX = 0x1C
TYPE_QR = 0x20

# QR special-function parameter: EBCDIC-to-ASCII conversion code pages
# that Python's stdlib can decode (byte 6 of the BDA, when byte 5 bit 0
# requests translation).
_CONVERSION_CODECS = {0x01: "cp500"}

_EC_LEVELS = "lmqh"  # BDA byte 8: 0x00-0x03 -> L M Q H


@dataclass
class BarCode:
    """One decoded bar code object, ready to generate."""

    bc_type: int
    modifier: int
    upi: int  # BSD units per inch (unit base 00 = per 10 inches)
    module_mils: int  # module width in thousandths of an inch
    data: str
    version: int = 0  # QR: desired symbol version, 0 = smallest
    ec_level: int = 0  # QR: 0-3 = L M Q H
    x: int = 0  # symbol origin within the object area, in BSD units
    y: int = 0


def parse_barcode(bdd: bytes, bda: bytes) -> Optional[BarCode]:
    """Combine BDD and BDA field data into a BarCode, or None if malformed.

    BSD layout (in the BDD): unit base(1) reserved(1) XUPUB(2) YUPUB(2)
    Xextent(2) Yextent(2) symbol width(2) type(1) modifier(1) HRI font(1)
    color(2) module width(1) element height(2) multiplier(1) WE:NE(2).

    BSA layout (in the BDA): flags(1) Xoffset(2) Yoffset(2), then for
    2D symbologies the special-function parameters, then the data.
    """
    if len(bdd) < 18 or len(bda) < 5:
        return None
    xupub = int.from_bytes(bdd[2:4], "big")
    if bdd[0] == 0x01:  # unit base X'01': units per 10 centimeters
        upi = round(xupub * 2.54 / 10)
    else:
        upi = xupub // 10  # unit base X'00': units per 10 inches
    bc_type = bdd[12]
    modifier = bdd[13]
    module_mils = bdd[17]
    if module_mils in (0x00, 0xFF):  # default: device specific
        module_mils = 13
    flags = bda[0]
    if flags & 0x04:  # bit 5: suppress the bar code symbol
        return None
    x = int.from_bytes(bda[1:3], "big")
    y = int.from_bytes(bda[3:5], "big")
    version = ec_level = 0
    payload = bda[5:]
    codec = "ascii"
    if bc_type == TYPE_QR and len(bda) >= 14:
        # QR special-function parameters (Table 31): control flags(5)
        # conversion(6) version(7) error correction(8) sequence(9)
        # total(10) parity(11) special flags(12) application(13) data(14-).
        if bda[5] & 0x80:  # EBCDIC-to-ASCII translation requested
            codec = _CONVERSION_CODECS.get(bda[6], "cp500")
        version = bda[7] if bda[7] <= 40 else 0
        ec_level = bda[8] if bda[8] <= 3 else 0
        if bda[10]:
            logger.info("QR structured append not supported; "
                        "rendering symbol %d/%d alone", bda[9], bda[10])
        payload = bda[14:]
    elif bc_type == TYPE_DATA_MATRIX and len(bda) >= 15:
        # Data Matrix special-function parameters (Table 23) run bytes
        # 5-14; the data characters start at byte 15.
        if bda[5] & 0x80:
            codec = _CONVERSION_CODECS.get(bda[6], "cp500")
        payload = bda[15:]
    try:
        data = payload.decode(codec)
    except (UnicodeDecodeError, LookupError):
        data = payload.decode("latin-1")
    return BarCode(
        bc_type=bc_type, modifier=modifier, upi=upi or 1440,
        module_mils=module_mils, data=data, version=version,
        ec_level=ec_level, x=x, y=y,
    )


def barcode_png(bar: BarCode) -> Optional[Tuple[bytes, int]]:
    """Generate the symbol as (PNG bytes, modules per side).

    Only QR is generated; unsupported symbologies return None so the
    caller can skip them instead of drawing something invented.
    """
    if bar.bc_type != TYPE_QR:
        if bar.bc_type == TYPE_DATA_MATRIX:
            logger.info("Data Matrix symbol not supported; skipping "
                        "rather than drawing a QR look-alike")
        else:
            logger.info("bar code type 0x%02X not supported", bar.bc_type)
        return None
    error = _EC_LEVELS[bar.ec_level]
    try:
        qr = segno.make_qr(bar.data, version=bar.version or None,
                           error=error, boost_error=False)
    except segno.DataOverflowError:
        # BSD asked for a too-small version; spec behavior for control
        # flag bit 2 = B'0' is to grow to the smallest symbol that fits.
        try:
            qr = segno.make_qr(bar.data, error=error, boost_error=False)
        except (segno.DataOverflowError, ValueError) as exc:
            logger.warning("cannot encode QR data: %s", exc)
            return None
    except ValueError as exc:
        logger.warning("cannot encode QR data: %s", exc)
        return None
    matrix = qr.matrix
    n = len(matrix)
    row_bytes = (n + 7) // 8
    raster = bytearray(b"\xff" * (row_bytes * n))  # PNG gray: 1 = white
    for r, row in enumerate(matrix):
        base = r * row_bytes
        for c, dark in enumerate(row):
            if dark:
                raster[base + c // 8] &= ~(0x80 >> (c % 8))
    return pack_png(n, n, 1, 0, row_bytes, bytes(raster)), n
