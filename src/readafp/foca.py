"""FOCA (Font Object Content Architecture) raster-font decoding.

An AFP font character set is bracketed by BFN...EFN and carries the
actual glyph shapes. For raster (bitmap) fonts the relevant fields are:

    FNC (Font Control)       resolution, pattern-data alignment
    FND (Font Descriptor)    typeface description name
    FNI (Font Index)         per character: GCGID, increment, FNM index
    FNM (Font Patterns Map)  per pattern: box width/height, data offset
    FNG (Font Patterns)      the concatenated 1-bit-per-pel bitmaps

This module groups those fields into Font objects whose glyph bitmaps
are rebuilt as PNGs (dark pel on white). Outline fonts (Type 1 / CID)
carry vendor shape data we do not interpret; they parse to metrics-only
Font objects with no glyph images.

Reference: Font Object Content Architecture Reference, AFPC-0001-06
(docs/specs/foca-reference-06.pdf), structured fields FNC/FNI/FNM/FNG.
"""

import logging
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from readafp import cff, type1
from readafp.ioca import pack_png
from readafp.parser import StructuredField

logger = logging.getLogger(__name__)

# FOCA structured-field identifiers (category X'89').
BFN = 0xD3A889  # Begin Font
EFN = 0xD3A989  # End Font
FNC = 0xD3A789  # Font Control
FND = 0xD3A689  # Font Descriptor
FNI = 0xD38C89  # Font Index
FNM = 0xD3A289  # Font Patterns Map
FNG = 0xD3EE89  # Font Patterns
FNN = 0xD3AB89  # Font Name Map (GCGID -> character name)

# FNC byte 1, Pattern Technology Identifier.
PATTECH_RASTER = 0x05  # Laser Matrix N-bit Wide (bitmap glyphs)
PATTECH_TYPE1 = 0x1E  # Adobe Type 1 (PFB) outline
PATTECH_CID = 0x1F  # CID-keyed outline

# Human-readable labels for the pattern technologies we recognize.
_PATTECH_LABELS = {
    PATTECH_RASTER: "laser-matrix raster",
    PATTECH_TYPE1: "Type 1 (PFB) outline",
    PATTECH_CID: "CID-keyed outline",
}

# Pattern-data alignment factor, keyed by FNC byte 16 (PatAlign).
_ALIGN = {0x00: 1, 0x02: 4, 0x03: 8}

# Bounds so a pathological font cannot blow up the render.
_MAX_GLYPHS = 1024
_MAX_CHARS = 4096  # FNI metric records (outline fonts have no bitmaps)
_MAX_PATTERN_BYTES = 1 << 20  # 1 MiB per glyph bitmap


@dataclass
class Glyph:
    """One raster glyph rebuilt as a PNG bitmap."""

    gcgid: str  # Graphic Character Global Identifier, e.g. "LF010000"
    width: int  # pels
    height: int
    char_increment: int  # inline advance, in the font's metric units
    png: bytes  # 1-bit grayscale PNG, dark pel on white


@dataclass
class CharMetric:
    """One character's identity and inline advance, with no shape data.

    Outline fonts carry these in the Font Index even though their glyph
    outlines are vendor data we do not rasterize.
    """

    gcgid: str  # Graphic Character Global Identifier
    char_increment: int  # inline advance, in the font's design units
    name: str = ""  # readable character name from the Font Name Map


@dataclass
class Font:
    """One BFN...EFN font character set."""

    name: str  # BFN token name
    typeface: str  # FND descriptive name, e.g. "TIMES-ROMAN"
    pattern_tech: int  # FNC PatTech
    glyphs: List[Glyph] = field(default_factory=list)
    chars: List[CharMetric] = field(default_factory=list)  # unique outline
    orientations: int = 1  # distinct rotations the FNI lists per character
    outline_format: str = ""  # detected from FNG payload, e.g. Type 1 PFB
    units_per_em: int = 0  # design-unit em for outline glyphs (0 = unknown)
    outline_glyphs: Dict[str, "type1.Glyph"] = field(default_factory=dict)

    @property
    def is_raster(self) -> bool:
        return self.pattern_tech == PATTECH_RASTER

    @property
    def is_outline(self) -> bool:
        return self.pattern_tech in (PATTECH_TYPE1, PATTECH_CID)

    @property
    def tech_label(self) -> str:
        """Human-readable technology, preferring the FNG-detected format."""
        if self.outline_format:
            return self.outline_format
        return _PATTECH_LABELS.get(
            self.pattern_tech, f"unknown (X'{self.pattern_tech:02X}')"
        )


def _decode_name(raw: bytes) -> str:
    """Decode an EBCDIC name field, trimmed to its printable run."""
    try:
        text = raw.decode("cp500")
    except UnicodeDecodeError:
        return ""
    out = []
    for ch in text:
        if ch.isprintable() and ch not in "\x00":
            out.append(ch)
        else:
            break
    return "".join(out).strip()


def _glyph_png(pattern: bytes, width: int, height: int) -> Optional[bytes]:
    """Build a 1-bpp PNG from a FOCA raster pattern (1 = toned/dark).

    PNG bit-depth-1 grayscale treats 0 as black, so each pel bit is
    inverted: a toned FOCA pel becomes a cleared (dark) PNG bit.
    """
    row_bytes = (width + 7) // 8
    if row_bytes * height > _MAX_PATTERN_BYTES:
        return None
    out = bytearray(b"\xff" * row_bytes * height)
    for ry in range(height):
        src = ry * row_bytes
        for col in range(row_bytes):
            i = src + col
            if i < len(pattern):
                out[ry * row_bytes + col] = pattern[i] ^ 0xFF
    return pack_png(width, height, 1, 0, row_bytes, bytes(out))


def _decode_raster_glyphs(
    fnc: bytes, fni: bytes, fnm: bytes, fng: bytes
) -> List[Glyph]:
    """Decode raster glyphs by joining FNI metrics to FNM/FNG patterns."""
    align = _ALIGN.get(fnc[16], 1) if len(fnc) > 16 else 1
    fni_rg = fnc[15] if len(fnc) > 15 else 28

    # FNM index -> GCGID/increment, from the Font Index.
    by_pattern: Dict[int, tuple] = {}
    if fni_rg >= 18:
        for i in range(0, len(fni) - fni_rg + 1, fni_rg):
            gcgid = _decode_name(fni[i : i + 8])
            char_inc = struct.unpack(">H", fni[i + 8 : i + 10])[0]
            fnm_index = struct.unpack(">H", fni[i + 16 : i + 18])[0]
            by_pattern.setdefault(fnm_index, (gcgid, char_inc))

    glyphs: List[Glyph] = []
    count = len(fnm) // 8
    for idx in range(min(count, _MAX_GLYPHS)):
        off = idx * 8
        box_w = struct.unpack(">H", fnm[off : off + 2])[0] + 1
        box_h = struct.unpack(">H", fnm[off + 2 : off + 4])[0] + 1
        pat_off = struct.unpack(">I", fnm[off + 4 : off + 8])[0] * align
        row_bytes = (box_w + 7) // 8
        pattern = fng[pat_off : pat_off + row_bytes * box_h]
        png = _glyph_png(pattern, box_w, box_h)
        if png is None:
            continue
        gcgid, char_inc = by_pattern.get(idx, ("", 0))
        glyphs.append(
            Glyph(
                gcgid=gcgid,
                width=box_w,
                height=box_h,
                char_increment=char_inc,
                png=png,
            )
        )
    return glyphs


def _fni_metrics(fnc: bytes, fni: bytes) -> List[CharMetric]:
    """Read GCGID + inline increment from the Font Index, ignoring shapes.

    Each FNI record is ``fnc[15]`` bytes (default 28): GCGID (8 EBCDIC
    bytes) then the 2-byte character increment. Outline fonts use the
    minimal 10-byte record with no Font Patterns Map index.
    """
    fni_rg = fnc[15] if len(fnc) > 15 else 28
    if fni_rg < 10:
        return []
    out: List[CharMetric] = []
    for i in range(0, len(fni) - fni_rg + 1, fni_rg):
        gcgid = _decode_name(fni[i : i + 8])
        char_inc = struct.unpack(">H", fni[i + 8 : i + 10])[0]
        out.append(CharMetric(gcgid=gcgid, char_increment=char_inc))
        if len(out) >= _MAX_CHARS:
            break
    return out


def _fnn_glyph_names(fnn: bytes) -> Dict[str, str]:
    """Map each GCGID to its readable character name from the Name Map.

    FNN is a 2-byte header, then 12-byte records (8-byte GCGID + a 4-byte
    offset), then a pool of length-prefixed names. Offsets index the whole
    FNN stream and the length byte counts itself, so an N-letter name has
    a stored length of N+1. The record table runs until it meets the name
    pool, where a "GCGID" no longer decodes to a letter-led token.
    """
    names: Dict[str, str] = {}
    i = 2
    while i + 12 <= len(fnn) and len(names) < _MAX_CHARS:
        gcgid = _decode_name(fnn[i : i + 8])
        off = struct.unpack(">I", fnn[i + 8 : i + 12])[0]
        i += 12
        if not gcgid or not gcgid[0].isalpha():
            break  # reached the name pool
        if off >= len(fnn):
            continue
        ln = fnn[off]
        if ln < 2:
            continue
        try:
            names[gcgid] = fnn[off + 1 : off + ln].decode("ascii")
        except UnicodeDecodeError:
            continue
    return names


def _is_cff(fng: bytes) -> bool:
    """True if the FNG payload is a CFF program (bare or OTTO-wrapped).

    A bare CFF opens with version (major=1, minor=0) and a header size of at
    least 4; an OpenType/CFF wrapper opens with the ``OTTO`` sfnt tag.
    """
    return (
        fng[:4] == b"OTTO"
        or (len(fng) >= 4 and fng[0] == 1 and fng[1] == 0 and 4 <= fng[2] <= 32)
    )


def _sniff_outline_format(fng: bytes) -> str:
    """Identify an outline font program from its FNG payload signature.

    The FNG bytes embed the vendor font program; its header reveals the
    real technology (e.g. an Adobe Type 1 PFB starts with the PostScript
    ``%!PS-AdobeFont`` banner). Returns "" when no signature is matched,
    so the caller falls back to the FNC pattern-technology label.
    """
    head = fng[:512]
    if b"%!PS-AdobeFont" in head or b".PFB" in head:
        return "Adobe Type 1 (PFB) outline"
    if fng[:4] == b"OTTO":
        return "CFF (OpenType) outline"
    if _is_cff(fng):
        return "CFF / CID-keyed outline"
    return ""


def _dedup_orientations(raw: List[CharMetric]) -> tuple:
    """Collapse per-orientation FNI records to one entry per character.

    Outline fonts list every character once per rotation (0/90/180/270),
    so a 374-glyph font yields 1496 FNI records. Keep the first (primary
    orientation) record per GCGID and report how many orientations the
    index carried.
    """
    seen: Dict[str, CharMetric] = {}
    for cm in raw:
        seen.setdefault(cm.gcgid, cm)
    chars = list(seen.values())
    orientations = len(raw) // len(chars) if chars else 1
    return chars, max(1, orientations)


def _decode_outlines(
    fng: bytes, chars: List[CharMetric]
) -> tuple:
    """Decode each character's outline from the embedded FNG font program.

    Dispatches on the program signature: a CFF program (bare or OTTO-
    wrapped) is interpreted with the Type 2 charstring engine in
    :mod:`readafp.cff`; otherwise an Adobe Type 1 (PFB) program is decoded
    with :mod:`readafp.type1`. Returns (units_per_em, {GCGID: Glyph}); on
    any failure the outlines come back empty and the caller falls back to a
    metrics-only specimen.
    """
    try:
        if _is_cff(fng):
            font = cff.CFFFont(fng)
        else:
            font = type1.Type1Font(fng)
    except (ValueError, IndexError, struct.error) as exc:
        logger.warning("Outline program decode failed: %s", exc)
        return 0, {}
    if not font.glyph_names:
        return 0, {}
    outlines: Dict[str, "type1.Glyph"] = {}
    for cm in chars:
        if cm.name and font.has_glyph(cm.name):
            glyph = font.glyph(cm.name)
            if glyph is not None:
                outlines[cm.gcgid] = glyph
    return font.units_per_em, outlines


_FNO = 0xD3AE89  # Font Orientation
_FNP = 0xD3AC89  # Font Position
_CPD = 0xD3A687  # Code Page Descriptor
_CPC = 0xD3A787  # Code Page Control
_CPI = 0xD38C87  # Code Page Index
_PATTECH_NAMES = {
    PATTECH_RASTER: "laser-matrix raster",
    PATTECH_TYPE1: "Type 1 outline",
    PATTECH_CID: "CID/Type 1 outline",
}


def describe_foca_field(field: "StructuredField") -> str:
    """A readable one-line summary of a FOCA font metric field, or "".

    Covers the fixed-format font fields that carry no triplets — FND (Font
    Descriptor), FNC (Font Control) and FNO (Font Orientation) — decoding
    the values an AFP inspector shows (face name, weight/width class, glyph
    box extent, pattern technology, advance). Offsets verified against a
    known font.
    """
    sid, d = field.sf_id, field.data
    if sid == FND and len(d) >= 36:  # Font Descriptor
        name = _decode_name(d[:32]) or "?"
        weight, width = d[32], d[33]
        max_vert = struct.unpack(">H", d[34:36])[0]
        return (f"FaceName={name} WeightClass={weight} WidthClass={width} "
                f"MaxVertSize={max_vert / 10:g}pt")
    if sid == FNC and len(d) >= 20:  # Font Control
        tech = _PATTECH_NAMES.get(d[1], f"0x{d[1]:02X}")
        max_w = struct.unpack(">H", d[10:12])[0] + 1
        max_h = struct.unpack(">H", d[12:14])[0] + 1
        psize = struct.unpack(">H", d[18:20])[0]
        return (f"MaxW={max_w} MaxH={max_h} PatternsSize={psize} "
                f"FontTech={tech}")
    if sid == _FNO and len(d) >= 10:  # Font Orientation
        rotation = struct.unpack(">H", d[0:2])[0]
        space_inc = struct.unpack(">H", d[8:10])[0]
        return f"CharRotation={rotation} SpaceCharInc={space_inc}"
    if sid == _FNP and len(d) >= 10:  # Font Position
        # MaxAscender/Descender are validated; the underscore fields are
        # all-zero in available fonts, so their offsets aren't pinned.
        ascender = struct.unpack(">H", d[6:8])[0]
        descender = struct.unpack(">H", d[8:10])[0]
        return f"MaxAscender={ascender} MaxDescender={descender}"
    if sid == _CPD and len(d) >= 38:  # Code Page Descriptor
        name = _decode_name(d[:32]) or "?"
        gcgid_len = struct.unpack(">H", d[32:34])[0]
        num_points = struct.unpack(">H", d[36:38])[0]
        cpgid = struct.unpack(">H", d[40:42])[0] if len(d) >= 42 else 0
        return (f"CodePage={name!r} NumCodePoints={num_points} "
                f"GCGIDLen={gcgid_len} CPGID={cpgid}")
    if sid == _CPC and len(d) >= 9:  # Code Page Control
        return f"DefaultChar={_decode_name(d[:8])} SpaceCharVal={d[8]}"
    if sid == _CPI:  # Code Page Index: code point -> GCGID
        cp = parse_code_page(d)
        if cp:
            sample = ", ".join(
                f"0x{k:02X}→{v}" for k, v in list(cp.items())[:3])
            more = f", +{len(cp) - 3} more" if len(cp) > 3 else ""
            return f"{len(cp)} code points: {sample}{more}"
    return ""


def parse_code_page(cpi: bytes) -> Dict[int, str]:
    """Map each code point to its GCGID from a Code Page Index (CPI).

    Single-byte CPI records are GCGID (8 EBCDIC bytes) + a section byte +
    the 1-byte code point. Returns {code_point: GCGID}; records whose
    GCGID does not decode are skipped, which also stops parsing cleanly at
    any trailing/padding bytes.
    """
    out: Dict[int, str] = {}
    rg = 10
    for i in range(0, len(cpi) - rg + 1, rg):
        gcgid = _decode_name(cpi[i : i + 8])
        if not gcgid or not gcgid[0].isalpha():
            continue
        out[cpi[i + 9]] = gcgid
    return out


def parse_fonts(fields: List[StructuredField]) -> List[Font]:
    """Extract every BFN...EFN font character set from a parsed file.

    Raster fonts gain decoded glyph bitmaps; outline (Type 1 / CID) fonts
    parse to a Font carrying its typeface, technology and FNI character
    metrics (GCGID + increment), but no glyph images — the vendor outline
    shapes are not rasterized.
    """
    fonts: List[Font] = []
    name = ""
    fnc = fnd = b""
    fni = fnm = fng = fnn = b""
    in_font = False

    for f in fields:
        if f.sf_id == BFN:
            in_font = True
            name = _decode_name(f.data[:8])
            fnc = fnd = b""
            fni = fnm = fng = fnn = b""
        elif not in_font:
            continue
        elif f.sf_id == FNC:
            fnc = f.data
        elif f.sf_id == FND:
            fnd = f.data
        elif f.sf_id == FNI:
            fni += f.data
        elif f.sf_id == FNM:
            fnm += f.data
        elif f.sf_id == FNG:
            fng += f.data
        elif f.sf_id == FNN:
            fnn += f.data
        elif f.sf_id == EFN:
            in_font = False
            tech = fnc[1] if len(fnc) > 1 else 0
            typeface = _decode_name(fnd[:32]) if fnd else ""
            typeface = typeface.split("@")[0].strip()  # drop GRID suffix
            glyphs: List[Glyph] = []
            chars: List[CharMetric] = []
            orientations = 1
            outline_format = ""
            units_per_em = 0
            outline_glyphs: Dict[str, "type1.Glyph"] = {}
            if tech == PATTECH_RASTER and fnm and fng:
                try:
                    glyphs = _decode_raster_glyphs(fnc, fni, fnm, fng)
                except (struct.error, IndexError) as exc:
                    logger.warning("FOCA glyph decode failed for %s: %s",
                                   name, exc)
            elif tech in (PATTECH_TYPE1, PATTECH_CID) and fni:
                try:
                    chars, orientations = _dedup_orientations(
                        _fni_metrics(fnc, fni)
                    )
                    glyph_names = _fnn_glyph_names(fnn) if fnn else {}
                    for cm in chars:
                        cm.name = glyph_names.get(cm.gcgid, "")
                except (struct.error, IndexError) as exc:
                    logger.warning("FOCA FNI metrics decode failed for "
                                   "%s: %s", name, exc)
                outline_format = _sniff_outline_format(fng)
                units_per_em, outline_glyphs = _decode_outlines(fng, chars)
            fonts.append(
                Font(
                    name=name,
                    typeface=typeface,
                    pattern_tech=tech,
                    glyphs=glyphs,
                    chars=chars,
                    orientations=orientations,
                    outline_format=outline_format,
                    units_per_em=units_per_em,
                    outline_glyphs=outline_glyphs,
                )
            )
    return fonts
