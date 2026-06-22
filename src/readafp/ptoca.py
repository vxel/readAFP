"""PTOCA presentation-text decoding and page extraction.

PTX structured fields carry chains of PTOCA control sequences:

    0x2B 0xD3            escape introducing a chain
    u8 length            counts the length byte, type byte and parameters
    u8 type              function type; low bit set = chained (the next
                         sequence follows immediately, without an escape)
    params               (length - 2 bytes)

Coordinates are in L-units along the inline (I, across the line) and
baseline (B, down the page) axes, scaled by the PGD/PTD units-per-base
(typically 14400 per 10 inches, i.e. 1440/inch).

Reference: PTOCA Reference, AFPC-0009-04 (docs/specs/ptoca-reference-04.pdf).
"""

import logging
from dataclasses import dataclass, field, replace
from typing import Dict, Iterator, List, Optional, Tuple

from readafp.bcoca import barcode_png, parse_barcode
from readafp.foca import Font, parse_code_page, parse_fonts
from readafp.gcgid import bridge_code_page
from readafp.goca import GocaGraphic, draw_goca
from readafp.ioca import cmyk_jpeg_bands, image_blob, parse_image_segment
from readafp.parser import StructuredField
from readafp.triplets import (
    iter_triplets,
    mcf_font_resources,
    parse_mcf_codepages,
)
from readafp.type1 import glyph_to_path_d

logger = logging.getLogger(__name__)

ESCAPE = b"\x2b\xd3"

# Control sequence function types, keyed by the unchained (even) value.
CS_NAMES = {
    0x74: "STC (Set Text Color)",
    0x80: "SEC (Set Extended Text Color)",
    0xC0: "SIM (Set Inline Margin)",
    0xC2: "SIA (Set Intercharacter Adjustment)",
    0xC4: "SVI (Set Variable Space Increment)",
    0xC6: "AMI (Absolute Move Inline)",
    0xC8: "RMI (Relative Move Inline)",
    0xD0: "SBI (Set Baseline Increment)",
    0xD2: "AMB (Absolute Move Baseline)",
    0xD4: "RMB (Relative Move Baseline)",
    0xD8: "BLN (Begin Line)",
    0xDA: "TRN (Transparent Data)",
    0xE4: "DIR (Draw I-axis Rule)",
    0xE6: "DBR (Draw B-axis Rule)",
    0xF0: "SCFL (Set Coded Font Local)",
    0xF2: "BSU (Begin Suppression)",
    0xF4: "ESU (End Suppression)",
    0xF6: "STO (Set Text Orientation)",
    0xF8: "NOP (No Operation)",
}

# STC two-byte standard color values -> CSS color.
_STC_COLORS = {
    0x0001: "#0000ff",  # blue
    0x0002: "#ff0000",  # red
    0x0003: "#ff00ff",  # pink/magenta
    0x0004: "#00ff00",  # green
    0x0005: "#00ffff",  # turquoise/cyan
    0x0006: "#ffff00",  # yellow
    0x0008: "#000000",  # black
    0x0010: "#a52a2a",  # brown
    0xFF07: "#000000",  # "color of medium" default
}

DEFAULT_COLOR = "#000000"

# Rough glyph metrics used only to advance the inline coordinate after a
# text run when the producer does not issue an explicit move. 240 L-units
# is 12pt at 1440 units/inch.
DEFAULT_FONT_SIZE = 240
_CHAR_ADVANCE_RATIO = 0.55

# Cap content per page so pathological files (perf_ptx.afp carries 65k
# PTX fields in one unbracketed document) produce a bounded SVG.
MAX_RUNS_PER_PAGE = 5000


@dataclass
class ControlSequence:
    """One decoded PTOCA control sequence."""

    cs_type: int  # unchained (even) function type
    params: bytes

    @property
    def name(self) -> str:
        return CS_NAMES.get(self.cs_type, f"Unknown (0x{self.cs_type:02X})")


@dataclass
class FidelityNote:
    """A reason the render of an element may differ from the original.

    ``cat`` groups notes so the viewer can filter them: ``font`` (substitute
    font, external/non-embedded reference, estimated size, stretched run),
    ``glyph`` (a code point we couldn't map and omitted), ``codepage``
    (decoded with a fallback/bridged code page), ``image`` (composite or
    scaling approximation). ``msg`` is a short human explanation.
    """

    cat: str
    msg: str


@dataclass
class FontInfo:
    """A font mapped to a local id by MDR/MCF, as far as we can decode it."""

    family: str = "Arial"
    weight: str = "normal"
    size: Optional[int] = None  # L-units (at 1440/inch, 1pt = 20 units)
    # Point size decoded from an external coded-font name when no MDR/raster
    # font declares one; resolved to L-units at render time (needs page upi).
    size_pt: Optional[float] = None
    notes: List[FidelityNote] = field(default_factory=list)


@dataclass
class TextRun:
    """A run of text positioned on a page, in page L-units."""

    x: int
    y: int
    text: str
    color: str = DEFAULT_COLOR
    font_id: Optional[int] = None
    font_size: int = DEFAULT_FONT_SIZE
    font_family: str = "Arial"
    font_weight: str = "normal"
    orientation: int = 0  # clockwise degrees (0/90/180/270) from STO
    src: Optional[int] = None  # offset of the PTX field that produced it
    fit: bool = True  # allow width-fitting; False for fixed synthetic layout
    # Width of the space character in L-units, from a preceding SVI (Set
    # Variable Space Increment). Producers vary it per line to justify text
    # (wide spaces fill the column; the last line drops to the natural
    # minimum). None when no SVI applies — spaces use the font default.
    space_width: Optional[int] = None
    notes: List[FidelityNote] = field(default_factory=list)


@dataclass
class ImageRef:
    """A raster image placed on a page, in page L-units."""

    x: int
    y: int
    width: int
    height: int
    mime: str
    data: bytes
    # CMYK plane JPEGs for band-interleaved images; data is empty then.
    bands: Optional[List[bytes]] = None
    # Scale without smoothing (bar code symbols: one pixel per module).
    crisp: bool = False
    # Recolor a 1-bit black-on-white glyph bitmap to this hex color (ink
    # becomes the color, the white background becomes transparent). Set for
    # embedded raster glyphs carrying an STC/SEC text color; None leaves the
    # bitmap untouched (IOCA photos, bar codes).
    recolor: Optional[str] = None
    notes: List[FidelityNote] = field(default_factory=list)
    # Page-space rotation (angle_deg, cx, cy) for rotated text orientations
    # (STO 90/180/270). None = upright; the renderer adds a rotate transform.
    rotate: Optional[tuple] = None


@dataclass
class Rule:
    """A solid rule (line) on a page, in page L-units."""

    x: int
    y: int
    length: int
    thickness: int
    axis: str  # "I" (horizontal) or "B" (vertical)
    color: str = DEFAULT_COLOR
    src: Optional[int] = None  # offset of the PTX field that produced it


@dataclass
class VectorGraphic:
    """A GOCA vector graphic placed on a page, in page L-units."""

    x: int
    y: int
    width: int   # bounding box in L-units (from OBD Object Area Size)
    height: int
    graphic: GocaGraphic  # SVG fragment in GPS-unit coordinate space
    # Page-space rotation (angle_deg, cx, cy) for rotated text orientations.
    rotate: Optional[tuple] = None


@dataclass
class Page:
    """One page's geometry and rough presentation-text content."""

    width: int = 12240  # letter at 1440/inch
    height: int = 15840
    units_per_inch: int = 1440
    texts: List[TextRun] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)
    images: List[ImageRef] = field(default_factory=list)
    graphics: List[VectorGraphic] = field(default_factory=list)
    # Decoded text for runs drawn as embedded glyph shapes/bitmaps rather
    # than <text>; not rendered, but kept so Copy-text / .txt export still
    # yields the page's words.
    text_layer: List[TextRun] = field(default_factory=list)
    truncated: bool = False  # content dropped after MAX_RUNS_PER_PAGE

    @property
    def plain_text(self) -> str:
        """Text runs joined in reading order (top-down, then left-right).

        Includes both substitute-font runs and the hidden text layer for
        runs that were drawn as embedded glyphs, so the extracted text is
        complete regardless of how each run was rendered.
        """
        ordered = sorted(self.texts + self.text_layer, key=lambda r: (r.y, r.x))
        lines: List[str] = []
        last_y: Optional[int] = None
        for run in ordered:
            if last_y is not None and abs(run.y - last_y) > 40:
                lines.append("\n")
            elif lines:
                lines.append(" ")
            lines.append(run.text)
            last_y = run.y
        return "".join(lines)


def iter_control_sequences(data: bytes) -> Iterator[ControlSequence]:
    """Yield PTOCA control sequences from PTX field data.

    Malformed tails are logged and skipped rather than raised: a partial
    decode of real-world PTX is more useful than none.
    """
    pos = 0
    chained = False
    while pos < len(data):
        if not chained:
            esc = data.find(ESCAPE, pos)
            if esc < 0:
                break
            pos = esc + 2
        if pos + 2 > len(data):
            logger.warning("truncated control sequence at offset %d", pos)
            break
        length, cs_type = data[pos], data[pos + 1]
        if length < 2 or pos + length > len(data):
            logger.warning(
                "bad control sequence length %d at offset %d", length, pos
            )
            break
        yield ControlSequence(
            cs_type=cs_type & 0xFE, params=bytes(data[pos + 2 : pos + length])
        )
        chained = bool(cs_type & 0x01)
        pos += length


def _decode_trn_counted(
    params: bytes, codepage: str = "cp500"
) -> Tuple[str, int, str, int]:
    """Decode TRN text, plus glyphs-stripped count and the codec used.

    Same decoding as :func:`_decode_trn`. The extra returns let callers
    raise fidelity notes: the count of code points dropped by
    :func:`_strip_controls` (producer symbol bytes the generic codec can't
    map) and the codec actually applied (``"utf-16-be"`` for TrueType flows,
    else the EBCDIC code page) so a "fallback code page" note never fires on
    a Unicode run.
    """
    if len(params) >= 2 and len(params) % 2 == 0:
        high_zeros = sum(1 for b in params[0::2] if b == 0)
        if high_zeros >= len(params) // 2 * 0.8:
            try:
                return params.decode("utf-16-be"), 0, "utf-16-be", 0
            except UnicodeDecodeError:
                pass
    used = codepage
    try:
        text = params.decode(codepage)
    except (UnicodeDecodeError, LookupError):
        text = params.decode("cp500", errors="replace")
        used = "cp500"
    # X'3F' is the EBCDIC SUBSTITUTE character (U+001A): the byte a producer
    # writes when it cannot encode a glyph in the AFP code page. Apache FOP
    # uses it two ways — a lone X'3F' amid real text is its list bullet
    # (confirmed against FOP's own AFP+PDF list output, so we render "•"), but
    # a run that is *mostly* X'3F' means the producer had no AFP font for those
    # glyphs and dropped them wholesale (e.g. a ZapfDingbats/Symbol specimen
    # FOP couldn't map becomes all X'3F'). Count them so the caller can flag a
    # dropped-glyph run rather than pretend it was a row of bullets.
    n_substitute = text.count("\x1a")
    text = text.replace("\x1a", "•")
    stripped = _strip_controls(text)
    return stripped, len(text) - len(stripped), used, n_substitute


def _decode_trn(params: bytes, codepage: str = "cp500") -> str:
    """Decode TRN text bytes: UTF-16BE for TrueType flows, else EBCDIC.

    ``codepage`` selects the EBCDIC decoder ring: the current font's
    MCF-labeled code page when the file declares one, else the user's
    choice. The UTF-16BE heuristic stays: text over Latin scripts has a
    zero high byte for nearly every character.
    """
    return _decode_trn_counted(params, codepage)[0]  # text only


def _strip_controls(text: str) -> str:
    """Drop C0/C1 control characters (keep tab/newline/CR and space).

    A byte that the active code page can't map to a real glyph often decodes
    to a control character (e.g. a producer's symbol code point our generic
    EBCDIC codec doesn't know — FOP encodes its list bullet at X'3F', which
    cp500 yields as U+001A). Rendering that as a tofu box □ misrepresents the
    page, so we omit it rather than fabricate a glyph we can't confirm.
    """
    if not any(ch < " " and ch not in "\t\n\r" or "\x7f" <= ch <= "\x9f"
               for ch in text):
        return text
    return "".join(
        ch for ch in text
        if ch in "\t\n\r" or (ch >= " " and not "\x7f" <= ch <= "\x9f")
    )


def _u16(b: bytes, off: int = 0) -> int:
    return int.from_bytes(b[off : off + 2], "big")


def _s16(b: bytes, off: int = 0) -> int:
    return int.from_bytes(b[off : off + 2], "big", signed=True)


def _substitute_font(typeface: str) -> Optional[Tuple[str, str]]:
    """Pick a CSS (family, weight) for a FOCA typeface, or None to skip.

    A document's embedded character set names its typeface (e.g.
    "COURIER", "TIMES-ROMAN", "TIMES-BOLD"); mapping that to a matching
    web font makes substitute text far closer to the original metrics than
    Arial, which also stops the inline-width fit from over-stretching it.
    """
    t = typeface.upper()
    weight = "bold" if "BOLD" in t else "normal"
    if "COURIER" in t or "MONO" in t:
        return "Courier New, monospace", weight
    if "TIMES" in t or "ROMAN" in t or "SERIF" in t:
        return "Times New Roman, serif", weight
    if "HELVETICA" in t or "ARIAL" in t or "SANS" in t:
        return "Arial, sans-serif", weight
    return None


# IBM / Apache FOP coded-font & character-set names encode the typeface in
# the 3rd character (e.g. C0H200B0 = Helvetica, C04200B0 = Courier,
# C0N200B0 = Times New Roman). When a font is *external* (not embedded and
# no MDR), this name is the only clue to whether it's serif/sans/mono.
_CODED_FONT_TYPEFACE = {
    "H": "HELVETICA", "4": "COURIER", "N": "TIMES", "T": "TIMES",
    "S": "TIMES", "5": "TIMES",
}


def _coded_font_substitute(name: str) -> Optional[Tuple[str, str]]:
    """Infer a substitute font from an IBM/FOP coded-font name, or None."""
    if len(name) >= 3 and name[:2] == "C0":
        return _substitute_font(_CODED_FONT_TYPEFACE.get(name[2], ""))
    return None


def _coded_font_point_size(name: str) -> Optional[float]:
    """Point size encoded in an IBM/FOP character-set name, or None.

    IBM raster character-set names like ``C0H200B0`` carry the point size in
    the 7th character: ``'0'`` (as in ``…00``) means 10 pt, otherwise
    ``10 + the letter's position in the alphabet`` — B=12, D=14, F=16, H=18 …
    Verified against the FOP fop-pairs' PDF ``Tf`` sizes (exact on 7 of 8
    pairs; only the scaled-text ``textdeko`` demo uses sizes not in any name).
    Only C0-prefixed names qualify, so it never fires on non-IBM names.
    """
    if len(name) < 7 or name[:2] != "C0":
        return None
    c = name[6]
    if c == "0":
        return 10.0
    if "A" <= c <= "Z":
        return 10.0 + (ord(c) - ord("A") + 1)
    return None


# Short label for a CSS font stack, for fidelity notes ("…rendered as Arial").
_FAMILY_LABEL = {
    "Arial, sans-serif": "Arial",
    "Times New Roman, serif": "Times New Roman",
    "Courier New, monospace": "Courier New",
}


def _typeface_label(cs_name: str, typefaces: Dict[str, str]) -> str:
    """Readable typeface for a character set: embedded name, else coded-font."""
    tf = typefaces.get(cs_name or "", "")
    if tf:
        return tf.title().replace("-", " ")
    if len(cs_name) >= 3 and cs_name[:2] == "C0":
        return _CODED_FONT_TYPEFACE.get(cs_name[2], "").title()
    return ""


def parse_mdr_fonts(data: bytes) -> Dict[int, FontInfo]:
    """Extract font name/size per local id from an MDR (Map Data Resource).

    The MDR body is repeating groups (u16 length includes itself), each
    holding triplets. For data-object fonts the useful ones are:

    - 0x02 FQN type 0xDE: the font's full name (e.g. "Arial Bold")
    - 0x8B data-object font descriptor: point size in 1/20 pt at offset 2,
      which at 1440 L-units/inch is the size in L-units directly
    - 0x02 FQN type 0xBE: the local id PTX SCFL sequences select
    """
    fonts: Dict[int, FontInfo] = {}
    pos = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            break
        name: Optional[str] = None
        size: Optional[int] = None
        local_id: Optional[int] = None
        for tid, tdata in iter_triplets(data[pos + 2 : pos + group_len]):
            if tid == 0x02 and len(tdata) >= 3:
                if tdata[0] == 0xDE:
                    name = _decode_trn(tdata[2:]).strip()
                elif tdata[0] == 0xBE:
                    local_id = tdata[-1]
            elif tid == 0x8B and len(tdata) >= 4:
                size = _u16(tdata, 2)
        if local_id is not None:
            family = name or "Arial"
            weight = "normal"
            for marker in (" Bold", " bold"):
                if marker in family:
                    family = family.replace(marker, "")
                    weight = "bold"
            fonts[local_id] = FontInfo(family=family, weight=weight, size=size)
        pos += group_len
    return fonts


def _sec_color(params: bytes) -> Optional[str]:
    """Decode an SEC (Set Extended Color) RGB value, if that's what it is."""
    # reserved(1) colorspace(1) reserved(4) sizes(4) value(...)
    if len(params) >= 13 and params[1] == 0x01 and params[6:9] == b"\x08\x08\x08":
        r, g, b = params[10], params[11], params[12]
        return f"#{r:02x}{g:02x}{b:02x}"
    return None


@dataclass
class _EmbeddedFont:
    """A coded font whose code page and glyphs are both embedded.

    Lets document text be drawn in the file's own font: each text byte
    resolves through ``cp_map`` (code point → GCGID) to a glyph. Raster
    fonts use ``glyphs`` (GCGID → foca.Glyph bitmap); outline fonts (Type 1
    / CFF) instead carry ``outline_glyphs`` (GCGID → type1.Glyph path) and
    a design-unit ``units_per_em`` so each glyph can be drawn as an SVG
    path scaled to the run's point size.
    """

    cp_map: Dict[int, str]
    glyphs: Dict[str, object]
    ref_height: int = 1  # tallest glyph box (pels): the uniform scale base
    outline_glyphs: Optional[Dict[str, object]] = None
    units_per_em: int = 0
    codec: Optional[str] = None  # codec for the hidden text-extraction layer
    resolution: int = 0  # raster font pattern resolution (pels/inch)
    point_size: float = 0.0  # raster font nominal size (points)


# A TRN run is drawn in the embedded font only when at least this fraction
# of its bytes resolve to a real glyph; below it the whole run falls back to
# a substitute font, so a few unbridged bytes (e.g. punctuation an external
# code page can't map) never riddle a word with blank gaps.
_EMBED_COVERAGE_MIN = 0.7

# Embedded *raster* glyphs are 1-bit bitmaps: crisp when large but thin and
# aliased once a small body font is scaled down to screen. So only draw them
# for display-size fonts (titles, headings) and let smaller text fall back to
# a clean substitute font. Outline (vector) glyphs have no such limit.
_EMBED_MIN_POINT_SIZE = 20.0


class _TextState:
    """Mutable PTOCA interpreter state, carried across PTX fields of a page."""

    def __init__(
        self,
        fonts: Optional[Dict[int, FontInfo]] = None,
        codepage: str = "cp500",
        font_codepages: Optional[Dict[int, str]] = None,
        embedded_text_fonts: Optional[Dict[int, _EmbeddedFont]] = None,
    ) -> None:
        self.codepage = codepage
        # Coded fonts whose code page and glyphs are embedded in the file,
        # so their text can be drawn in the document's own raster font.
        self.embedded_text_fonts = (
            embedded_text_fonts if embedded_text_fonts is not None else {}
        )
        # Per-coded-font code pages from MCF labels; the plain codepage
        # is the fallback for fonts the file leaves unlabeled.
        self.font_codepages = font_codepages if font_codepages is not None else {}
        self.i = 0
        self.b = 0
        self.inline_margin = 0
        self.baseline_increment = 0
        # Variable-space character increment (SVI), in L-units. None means
        # no SVI seen, so spaces fall back to the font's own advance.
        self.space_increment: Optional[int] = None
        self.color = DEFAULT_COLOR
        self.font_id: Optional[int] = None
        self.field_offset: Optional[int] = None
        # For implicit pages (no BPG/EPG): flow text and wrap at this
        # inline position, like a text dump, instead of letting runs
        # without explicit moves pile up on one endless line.
        self.wrap_width: Optional[int] = None
        # STO inline direction as clockwise degrees (0=right,90=down,
        # 180=left,270=up); also determines coordinate axis mapping.
        self.orientation: int = 0
        # Keep the caller's dict: it may be filled by MDRs seen later
        # (the page's AEG comes after BPG).
        self.fonts = fonts if fonts is not None else {}

    def apply(self, cs: ControlSequence, page: Page) -> None:
        t, p = cs.cs_type, cs.params
        if t == 0xC6 and len(p) >= 2:  # AMI
            self.i = _u16(p)
        elif t == 0xC8 and len(p) >= 2:  # RMI
            self.i += _s16(p)
        elif t == 0xD2 and len(p) >= 2:  # AMB
            self.b = _u16(p)
        elif t == 0xD4 and len(p) >= 2:  # RMB
            self.b += _s16(p)
        elif t == 0xC0 and len(p) >= 2:  # SIM
            self.inline_margin = _u16(p)
        elif t == 0xC4 and len(p) >= 2:  # SVI: set variable-space increment
            self.space_increment = _u16(p)
        elif t == 0xD0 and len(p) >= 2:  # SBI
            self.baseline_increment = _s16(p)
        elif t == 0xD8:  # BLN
            self.b += self.baseline_increment
            self.i = self.inline_margin
        elif t == 0xF0 and len(p) >= 1:  # SCFL
            self.font_id = p[0]
        elif t == 0x74 and len(p) >= 2:  # STC
            self.color = _STC_COLORS.get(_u16(p), DEFAULT_COLOR)
        elif t == 0x80:  # SEC
            self.color = _sec_color(p) or self.color
        elif t == 0xF6 and len(p) >= 2:  # STO
            # INLORENT is a u16 in units of 1/128 degree, CW from +X.
            # Standard values: 0=right, 11520=90°CW=down, 23040=180°=left,
            # 34560=270°CW=up.  Position resets to the page origin.
            self.orientation = (_u16(p) // 128) % 360
            self.i = 0
            self.b = 0
        elif t == 0xDA:  # TRN
            info = self.fonts.get(self.font_id, FontInfo())
            # Prefer an MDR-declared L-unit size; else a point size decoded
            # from the coded-font name, scaled to the page resolution; else a
            # ~12pt default.
            size = (
                info.size
                or (round(info.size_pt * page.units_per_inch / 72)
                    if info.size_pt else 0)
                or max(page.units_per_inch // 6, 8)
            )
            emb = self.embedded_text_fonts.get(self.font_id)
            if emb is not None:
                drawn = (
                    self._emit_embedded_outlines(page, p, emb, size)
                    if emb.outline_glyphs
                    else self._emit_embedded_glyphs(page, p, emb, size)
                )
                if drawn:
                    return
                # Too few bytes resolved to embedded glyphs (e.g. a run that
                # is mostly punctuation an external code page can't bridge):
                # fall through and draw the whole run in a substitute font.
            # Default size is ~12pt in the page's own resolution (1440/inch
            # gives 240; FOP emits 240/inch where 12pt is just 40).
            cp = self.font_codepages.get(self.font_id, self.codepage)
            text, n_stripped, used_codec, n_sub = _decode_trn_counted(p, cp)
            if (
                self.wrap_width is not None
                and self.i > 0
                and self.i + len(text) * size * _CHAR_ADVANCE_RATIO
                > self.wrap_width
            ):
                self.i = 240
                self.b += int(size * 1.4)
            if text.strip():
                if len(page.texts) < MAX_RUNS_PER_PAGE:
                    # For 90°/270°: inline is the vertical axis and
                    # baseline is horizontal, so swap to page coords.
                    if self.orientation in (90, 270):
                        tx, ty = self.b, self.i
                    else:
                        tx, ty = self.i, self.b
                    notes = list(info.notes)
                    if n_stripped:
                        notes.append(FidelityNote(
                            "glyph",
                            f"{n_stripped} character(s) omitted — code "
                            f"point(s) the {used_codec} codec can't map "
                            f"(usually a producer-specific symbol)."))
                    # A run that is mostly EBCDIC substitute chars (X'3F') is
                    # one whose glyphs the producer had no AFP font for and
                    # dropped wholesale — e.g. a ZapfDingbats/Symbol specimen
                    # FOP couldn't map. Flag it so the blanks/placeholders are
                    # explained rather than read as real content.
                    nonspace = sum(1 for ch in text if ch != " ")
                    if n_sub >= 3 and nonspace and n_sub / nonspace > 0.5:
                        notes.append(FidelityNote(
                            "glyph",
                            f"{n_sub} glyph(s) the producer couldn't encode in "
                            f"the AFP code page (wrote substitute X'3F') — the "
                            f"original glyphs (e.g. symbol/dingbat font) are "
                            f"absent from this AFP, not just unrendered here."))
                    if (used_codec != "utf-16-be"
                            and self.font_id not in self.font_codepages):
                        notes.append(FidelityNote(
                            "codepage",
                            f"No code page declared for this font — text "
                            f"decoded with the fallback {used_codec}."))
                    # A run carries its SVI space width only when the
                    # producer set one and the run actually has spaces to
                    # widen; otherwise spaces use the substitute font default.
                    sw = self.space_increment if " " in text else None
                    page.texts.append(
                        TextRun(
                            x=tx,
                            y=ty,
                            text=text,
                            color=self.color,
                            font_id=self.font_id,
                            font_size=size,
                            font_family=info.family,
                            font_weight=info.weight,
                            orientation=self.orientation,
                            src=self.field_offset,
                            space_width=sw,
                            notes=notes,
                        )
                    )
                else:
                    page.truncated = True
            # Advance the pen: non-space glyphs by the flat estimate, spaces
            # by the SVI width when one is in effect (so a justified line's
            # later runs still land in the right place).
            n_space = text.count(" ")
            glyph_adv = (len(text) - n_space) * size * _CHAR_ADVANCE_RATIO
            space_adv = n_space * (
                self.space_increment
                if self.space_increment is not None
                else size * _CHAR_ADVANCE_RATIO
            )
            self.i += int(glyph_adv + space_adv)
        elif t == 0xE4 and len(p) >= 2:  # DIR: horizontal rule
            self._add_rule(page, p, axis="I")
        elif t == 0xE6 and len(p) >= 2:  # DBR: vertical rule
            self._add_rule(page, p, axis="B")

    def _add_rule(self, page: Page, p: bytes, axis: str) -> None:
        if len(page.rules) >= MAX_RUNS_PER_PAGE:
            page.truncated = True
            return
        length = _s16(p)
        thickness = _s16(p, 2) if len(p) >= 4 else 20
        # Keep hairlines visible once scaled, but the floor must track the
        # page resolution: a fixed 10 is ~0.5pt at 1440/inch (fine) yet 3pt
        # at FOP's 240/inch (6x too thick). Round upi/144 to a ~0.5pt floor —
        # 2 units at 240/inch (solid, not faint) and 10 at 1440 (unchanged).
        min_thick = max(1, round(page.units_per_inch / 144))
        if 0 < abs(thickness) < min_thick:
            thickness = min_thick if thickness >= 0 else -min_thick
        page.rules.append(
            Rule(
                x=self.i,
                y=self.b,
                length=length,
                thickness=thickness,
                axis=axis,
                color=self.color,
                src=self.field_offset,
            )
        )

    def _embed_covers(
        self, data: bytes, emb: "_EmbeddedFont", glyphs: Dict[str, object]
    ) -> bool:
        """True if enough of ``data`` resolves to real embedded glyphs.

        Empty/blank runs count as covered (nothing to draw). Otherwise the
        fraction of bytes whose code-page GCGID has a glyph must reach
        ``_EMBED_COVERAGE_MIN``, else the caller draws a substitute instead.
        """
        if not data:
            return True
        mapped = sum(1 for byte in data if emb.cp_map.get(byte) in glyphs)
        return mapped / len(data) >= _EMBED_COVERAGE_MIN

    def _record_embedded_text(
        self, page: Page, data: bytes, emb: "_EmbeddedFont", x: int, y: int
    ) -> None:
        """Keep a run's decoded text for export when it is drawn as glyphs."""
        if not emb.codec:
            return
        try:
            text = data.decode(emb.codec, errors="replace")
        except LookupError:
            return
        if text.strip():
            page.text_layer.append(
                TextRun(x=x, y=y, text=text, font_id=self.font_id,
                        src=self.field_offset)
            )

    def _emit_embedded_glyphs(
        self, page: Page, data: bytes, emb: "_EmbeddedFont", size: int
    ) -> bool:
        """Draw a TRN run with the file's own embedded raster glyphs.

        Each byte resolves through the code page to a GCGID and its glyph
        bitmap. Pattern pels map to page L-units by the font's resolution
        (``upi / pels-per-inch``); the inline pen advances by the glyph's
        FNI character increment (in 1000ths of an em) scaled by the em in
        L-units (``point_size/72 × upi``) — so spacing follows the real
        metrics, not the bitmap width. Falls back to the older box-height
        scaling when a font omits resolution/point-size. Returns False
        without drawing when the font is too small for crisp bitmaps or too
        few bytes resolve, so the caller can substitute the whole run.
        """
        # Small raster fonts look rough scaled down; only large display
        # fonts (titles/headings) render as bitmaps, the rest substitute.
        if emb.point_size < _EMBED_MIN_POINT_SIZE:
            return False
        if not self._embed_covers(data, emb, emb.glyphs):
            return False
        self._record_embedded_text(page, data, emb, self.i, self.b)
        # Lay the glyphs out in the run's local horizontal frame anchored at
        # the page-space origin, then rotate every glyph around that shared
        # origin for STO 90/180/270 — one rotation carries both the glyph
        # rotation and the inline direction. The origin swaps axes for
        # 90/270, matching the substitute-text path.
        ox, oy, rot = self._oriented_origin()
        upi = page.units_per_inch
        # Pattern pel → L-unit. With a known resolution the point-size
        # factors cancel to a plain upi/resolution ratio; otherwise fall
        # back to mapping the tallest glyph box to the run size.
        pel = (upi / emb.resolution) if emb.resolution else (
            size / (emb.ref_height or 1))
        # Em → L-unit, for the 1000/em character increments.
        em = round(emb.point_size / 72 * upi) if emb.point_size else size
        default_adv = max(1, round(0.5 * em))  # advance for an unmapped byte
        # Honor an STC/SEC text color on the 1-bit glyph bitmaps: a non-default
        # color recolors the ink (and drops the white box) at render time.
        # Black needs no recolor — the bitmap is already black-on-white.
        recolor = self.color if self.color and self.color != DEFAULT_COLOR else None
        lx = 0  # inline offset from the origin, along the local +x axis
        for byte in data:
            if len(page.images) >= MAX_RUNS_PER_PAGE:
                page.truncated = True
                break
            gcgid = emb.cp_map.get(byte)
            glyph = emb.glyphs.get(gcgid) if gcgid else None
            if glyph is None:
                lx += default_adv
                continue
            inc = getattr(glyph, "char_increment", 0)
            adv = round(inc / 1000 * em) if inc else default_adv
            png = getattr(glyph, "png", None)
            if png and glyph.height and glyph.width:
                w = max(1, round(glyph.width * pel))
                h = max(1, round(glyph.height * pel))
                # The glyph box bottom sits below the baseline by its FNI
                # baseline offset (1000/em), so descenders (g, p, q, y) drop
                # under the line instead of resting on it.
                drop = round(getattr(glyph, "baseline_offset", 0) / 1000 * em)
                page.images.append(
                    ImageRef(
                        x=ox + lx, y=oy - h + drop, width=w, height=h,
                        mime="image/png", data=png, crisp=True,
                        recolor=recolor, rotate=rot,
                    )
                )
            lx += max(adv, 1)
        self.i += lx
        return True

    def _emit_embedded_outlines(
        self, page: Page, data: bytes, emb: "_EmbeddedFont", size: int
    ) -> bool:
        """Draw a TRN run with the file's own embedded outline glyphs.

        Each byte resolves through the code page to a GCGID and its outline
        (Type 1 or CFF). The whole run is emitted as one vector graphic: a
        single ``<path>`` of every glyph laid out in the font's design-unit
        space, then scaled so the em maps to the run's point size. Unlike
        the raster path, advances are exact — each glyph steps by its own
        design-unit advance width. Returns False without drawing when too
        few bytes resolve, so the caller can substitute.
        """
        if not self._embed_covers(data, emb, emb.outline_glyphs):
            return False
        self._record_embedded_text(page, data, emb, self.i, self.b)
        if len(page.graphics) >= MAX_RUNS_PER_PAGE:
            page.truncated = True
            return True
        em = emb.units_per_em or 1000
        ascent = round(0.90 * em)   # baseline depth from the box top
        box_h = round(1.20 * em)    # headroom for caps + descenders
        space = round(0.5 * em)     # advance for a byte with no glyph
        scale = size / em           # design units → L-units
        paths: List[str] = []
        x_design = 0.0
        for byte in data:
            gcgid = emb.cp_map.get(byte)
            glyph = emb.outline_glyphs.get(gcgid) if gcgid else None
            segments = getattr(glyph, "segments", None)
            if not segments:
                x_design += space
                continue
            paths.append(glyph_to_path_d(glyph, scale=1, ox=x_design, oy=ascent))
            x_design += getattr(glyph, "advance", 0) or space
        if paths:
            fill = self.color or DEFAULT_COLOR
            ox, oy, rot = self._oriented_origin()
            page.graphics.append(
                VectorGraphic(
                    x=ox,
                    y=oy - round(ascent * scale),
                    width=max(1, round(x_design * scale)),
                    height=max(1, round(box_h * scale)),
                    graphic=GocaGraphic(
                        svg=f'<path d="{"".join(paths)}" fill="{fill}"/>',
                        gps_w=max(1, round(x_design)),
                        gps_h=box_h,
                    ),
                    rotate=rot,
                )
            )
        self.i += round(x_design * scale)
        return True

    def _oriented_origin(self) -> tuple:
        """Page-space run origin and rotation for the current STO orientation.

        Returns ``(ox, oy, rot)`` where glyphs are laid out from ``(ox, oy)``
        along the local +x axis and ``rot`` is ``(angle, ox, oy)`` (or None at
        0°). For 90°/270° the inline axis is vertical, so the I/B scalars swap
        into page coordinates — the same convention as the substitute-text
        path, so embedded and substitute runs rotate identically.
        """
        o = self.orientation
        if o in (90, 270):
            ox, oy = self.b, self.i
        else:
            ox, oy = self.i, self.b
        return ox, oy, ((o, ox, oy) if o else None)


def _estimate_font_sizes(page: Page, known_fonts: Dict[int, FontInfo]) -> None:
    """Set each text run's font size from observed inter-run spacing.

    Fallback for fonts whose size was not declared by an MDR descriptor:
    most producers position every run with an explicit move, so the gap
    between two consecutive runs on the same baseline, divided by the
    first run's character count (+1 for the implied space), approximates
    that font's character width — and Latin text averages roughly half
    the point size.
    """
    sized = {fid for fid, info in known_fonts.items()
             if info.size or info.size_pt}
    # Clamp to ~4pt..60pt in the page's own resolution.
    lo = max(page.units_per_inch // 18, 4)
    hi = page.units_per_inch * 5 // 6

    # Primary estimate: baseline pitch. A font's consecutive baselines
    # are line-spaced at ~1.2x its point size, and column gaps can't
    # pollute the vertical axis the way they do horizontal gaps.
    baselines: dict = {}
    for r in page.texts:
        if r.font_id not in sized:
            baselines.setdefault(r.font_id, set()).add(r.y)
    pitch_est: dict = {}
    for fid, ys in baselines.items():
        ordered = sorted(ys)
        diffs = [
            b - a
            for a, b in zip(ordered, ordered[1:])
            if 0 < b - a <= page.units_per_inch // 2
        ]
        if diffs:
            diffs.sort()
            pitch = diffs[len(diffs) // 2]
            pitch_est[fid] = max(lo, min(hi, int(pitch / 1.2)))

    # Fallback for single-line fonts: horizontal gaps that imply a
    # plausible character width (~5pt-35pt; larger means column jump).
    min_char = page.units_per_inch / 30
    max_char = page.units_per_inch / 4
    samples: dict = {}
    for a, b in zip(page.texts, page.texts[1:]):
        if (
            a.y == b.y
            and b.x > a.x
            and a.text
            and a.font_id not in sized
            and a.font_id not in pitch_est
        ):
            per_char = (b.x - a.x) / (len(a.text) + 1)
            if min_char <= per_char <= max_char:
                samples.setdefault(a.font_id, []).append(per_char)

    for run in page.texts:
        if run.font_id in sized:
            continue  # MDR declared an exact size; nothing approximated
        inferred = False
        if run.font_id in pitch_est:
            run.font_size = pitch_est[run.font_id]
            inferred = True
        else:
            widths = samples.get(run.font_id)
            if widths:
                widths.sort()
                median = widths[len(widths) // 2]
                run.font_size = max(lo, min(hi, int(median / 0.52)))
                inferred = True
        # Fidelity: the producer didn't declare this font's point size.
        pt = round(run.font_size / page.units_per_inch * 72)
        if inferred:
            msg = (f"Point size not declared — estimated ≈{pt}pt from "
                   f"the producer's line spacing.")
        else:
            msg = (f"Point size not declared and not inferable (single "
                   f"line) — using a default of ≈{pt}pt.")
        if not any(n.msg == msg for n in run.notes):
            run.notes.append(FidelityNote("font", msg))


def _annotate_image_notes(pages: List[Page]) -> None:
    """Note images rendered through an optical/scaling approximation."""
    for page in pages:
        for img in page.images:
            if img.bands and not any(n.cat == "image" for n in img.notes):
                img.notes.append(FidelityNote(
                    "image",
                    "CMYK image composited from 4 ink planes via SVG "
                    "filters + multiply blend — on-screen colours are an "
                    "optical approximation of the printed inks."))


_IMAGE_MAGICS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
)


def _sniff_image(data: bytes) -> Optional[str]:
    """Return the MIME type if the bytes look like a known raster format."""
    for magic, mime in _IMAGE_MAGICS:
        if data.startswith(magic):
            return mime
    return None


@dataclass
class _ImageObject:
    """A decoded BIM...EIM image object, ready to place or page."""

    mime: str
    blob: bytes
    upi: int  # object-area units per inch, from its OBD (or image dpi)
    width: int  # object-area size in those units
    height: int
    x: int = 0  # OBP offsets, in the including page's units
    y: int = 0
    bands: Optional[List[bytes]] = None  # CMYK plane JPEGs


def _parse_obd(data: bytes) -> Tuple[Optional[int], int, int]:
    """Return (units_per_inch, width, height) from OBD triplets.

    0x4B (Measurement Units) gives units per unit base (00 = 10 inches);
    0x4C (Object Area Size) gives the extent in those units.
    """
    upi: Optional[int] = None
    width = height = 0
    for tid, tdata in iter_triplets(data):
        if tid == 0x4B and len(tdata) >= 4:
            upi = _u16(tdata, 2) // 10 or None
        elif tid == 0x4C and len(tdata) >= 7:
            width = int.from_bytes(tdata[1:4], "big")
            height = int.from_bytes(tdata[4:7], "big")
    return upi, width, height


def _parse_obp(data: bytes) -> Tuple[int, int]:
    """Return the object area's (x, y) origin from an OBP field."""
    # OAPosID(1) RGLength(1) XoaOSet(3) YoaOSet(3) ...
    if len(data) < 8:
        return 0, 0
    x = int.from_bytes(data[2:5], "big", signed=True)
    y = int.from_bytes(data[5:8], "big", signed=True)
    return x, y


def _finish_image_object(
    ipd: bytes, obd: Optional[bytes], obp: Optional[bytes]
) -> Optional[_ImageObject]:
    """Decode a completed BIM...EIM capture into a placeable object."""
    segment = parse_image_segment(ipd)
    if segment is None:
        return None
    bands: Optional[List[bytes]] = None
    blob = image_blob(segment)
    if blob is not None:
        mime, payload = blob
    else:
        bands = cmyk_jpeg_bands(segment)
        if bands is None:
            logger.info(
                "skipping IOCA image: compression 0x%02X, %d bits/IDE "
                "not decoded",
                segment.compression,
                segment.bits,
            )
            return None
        mime, payload = "image/jpeg", b""
    upi, width, height = _parse_obd(obd) if obd else (None, 0, 0)
    if width <= 0 or height <= 0:
        # No usable object area: fall back to the pixel grid at the
        # image's own resolution (0x94 resolution is per 10 inches).
        upi = segment.hres // 10 or None
        width, height = segment.width, segment.height
    if width <= 0 or height <= 0:
        return None
    x, y = _parse_obp(obp) if obp else (0, 0)
    return _ImageObject(
        mime=mime, blob=payload, upi=upi or 1440, width=width,
        height=height, x=x, y=y, bands=bands,
    )


def _scale(value: int, from_upi: int, to_upi: int) -> int:
    return value * to_upi // from_upi if from_upi else value


def _parse_iob(
    data: bytes,
    resources: Dict[str, bytes],
    images: Dict[str, _ImageObject],
    page_upi: int,
) -> Optional[ImageRef]:
    """Build an ImageRef from an IOB (Include Object) field, if renderable.

    IOB layout: name(8) reserved(1) ObjType(1) XoaOset(3) YoaOset(3)
    orientation(4) XocaOset(3) YocaOset(3) RefCSys(1), then triplets —
    0x4C (Object Area Size) carries the placed extent in the units of
    0x4B (Measurement Units), defaulting to the page's own units. The
    name resolves an object-container resource (raster bytes sniffed
    from OCD data) or a decoded IOCA image object.
    """
    if len(data) < 27:
        return None
    try:
        name = data[:8].decode("cp500").strip()
    except UnicodeDecodeError:
        return None
    x = int.from_bytes(data[10:13], "big", signed=True)
    y = int.from_bytes(data[13:16], "big", signed=True)
    upi: Optional[int] = None
    width = height = 0
    for tid, tdata in iter_triplets(data[27:]):
        if tid == 0x4C and len(tdata) >= 7:  # Object Area Size
            width = int.from_bytes(tdata[1:4], "big")
            height = int.from_bytes(tdata[4:7], "big")
        elif tid == 0x4B and len(tdata) >= 4:  # Measurement Units
            upi = _u16(tdata, 2) // 10 or None

    bands: Optional[List[bytes]] = None
    blob = resources.get(name)
    if blob is not None:
        mime = _sniff_image(blob)
        if mime is None:
            return None
    else:
        obj = images.get(name)
        if obj is None:
            return None
        mime, blob, bands = obj.mime, obj.blob, obj.bands
        if width <= 0 or height <= 0:  # fall back to the object's OBD
            upi, width, height = obj.upi, obj.width, obj.height
    if width <= 0 or height <= 0:
        return None
    return ImageRef(
        x=x,
        y=y,
        width=_scale(width, upi or page_upi, page_upi),
        height=_scale(height, upi or page_upi, page_upi),
        mime=mime,
        data=blob,
        bands=bands,
    )


def _parse_pgd(data: bytes) -> Tuple[int, int, int]:
    """Return (width, height, units_per_inch) from PGD field data."""
    # XpgBase(1) YpgBase(1) XpgUnits(2) YpgUnits(2) XpgSize(3) YpgSize(3)
    units = _u16(data, 2)
    width = int.from_bytes(data[6:9], "big")
    height = int.from_bytes(data[9:12], "big")
    units_per_inch = units // 10 if units else 1440  # unit base 00 = 10 in
    return width, height, units_per_inch


def _scan_code_pages(fields: List[StructuredField]) -> Dict[str, Dict[int, str]]:
    """Map each embedded code page (BCP...ECP) to its code-point → GCGID."""
    out: Dict[str, Dict[int, str]] = {}
    name: Optional[str] = None
    cpi = b""
    for f in fields:
        if f.sf_id == 0xD3A887:  # BCP
            name = (f.token_name or "").strip()
            cpi = b""
        elif f.sf_id == 0xD38C87:  # CPI
            cpi += f.data
        elif f.sf_id == 0xD3A987:  # ECP
            if name and cpi:
                out[name] = parse_code_page(cpi)
            name, cpi = None, b""
    return out


def extract_pages(
    fields: List[StructuredField], codepage: str = "cp500"
) -> List[Page]:
    """Walk a parsed document and build a rough page model per BPG...EPG.

    PTX fields outside any page bracket (some synthetic files put text
    directly under BDT) are collected onto one implicit page, appended
    after the bracketed pages and grown to fit its content.

    IOCA image objects (BIM...EIM) are decoded wherever they appear:
    inline in a page they are placed by their own OBP/OBD, in a
    resource group or page segment they are registered by name for IOB
    inclusion, and in a document with no pages at all (standalone
    object files) each becomes its own page-sized view.
    """
    pages: List[Page] = []
    current: Optional[Page] = None
    state: Optional[_TextState] = None
    implicit: Optional[Page] = None
    implicit_state: Optional[_TextState] = None
    pgd_default: Optional[Tuple[int, int, int]] = None
    fonts: Dict[int, FontInfo] = {}
    font_codepages: Dict[int, str] = {}
    # Embedded font resources, for drawing text in the file's own raster
    # glyphs: code pages (name → code-point→GCGID) and character sets
    # (name → GCGID→glyph). Pre-scanned so they are ready when an MCF maps
    # a local id to them. embedded_text_fonts is filled by the MCF handler
    # and shared by reference with each _TextState.
    code_pages = _scan_code_pages(fields)
    _parsed_fonts = parse_fonts(fields)
    char_set_glyphs = {
        font.name: {g.gcgid: g for g in font.glyphs}
        for font in _parsed_fonts
        if font.glyphs and font.name
    }
    # Raster char-set metrics (resolution dpi, point size) for sizing and
    # advancing the embedded bitmap glyphs at their true scale.
    char_set_metrics = {
        font.name: (font.resolution, font.point_size)
        for font in _parsed_fonts
        if font.glyphs and font.name
    }
    # Outline (Type 1 / CFF) character sets: GCGID → outline glyph + the
    # font's design-unit em, for drawing document text as real glyph paths.
    char_set_outlines = {
        font.name: (font.outline_glyphs, font.units_per_em)
        for font in _parsed_fonts
        if font.outline_glyphs and font.name
    }
    # Even when a char set's glyphs can't be drawn (its code page is
    # external), its typeface tells us a better substitute font than Arial.
    char_set_typefaces = {
        font.name: font.typeface
        for font in _parsed_fonts
        if font.name and font.typeface
    }
    embedded_text_fonts: Dict[int, _EmbeddedFont] = {}
    resources: Dict[str, bytes] = {}
    container: Optional[str] = None
    image_resources: Dict[str, _ImageObject] = {}
    loose_images: List[_ImageObject] = []
    resource_name: Optional[str] = None  # enclosing BRS or BPS token
    overlays: Dict[str, Page] = {}  # BMO...EMO content, by name, for IPO
    mpo_map: Dict[int, str] = {}  # MPO: overlay local id -> name, for IPO-by-id
    in_overlay = False
    overlay_name: Optional[str] = None
    in_image = False
    image_ipd = bytearray()
    image_obd: Optional[bytes] = None
    image_obp: Optional[bytes] = None
    image_name: Optional[str] = None
    in_barcode = False
    barcode_bdd = b""
    barcode_bdas: List[bytes] = []
    in_graphic = False
    graphic_gdd = b""
    graphic_gads: List[bytes] = []

    for f in fields:
        if f.sf_id == 0xD3A892:  # BOC opens an object container resource
            container = f.token_name
        elif f.sf_id == 0xD3A992:  # EOC
            container = None
        elif f.sf_id == 0xD3EE92 and container:  # OCD carries its bytes
            resources[container] = resources.get(container, b"") + f.data
        elif f.sf_id in (0xD3A8CE, 0xD3A85F):  # BRS / BPS name a resource
            resource_name = f.token_name
        elif f.sf_id in (0xD3A9CE, 0xD3A95F):  # ERS / EPS
            resource_name = None
        elif f.sf_id == 0xD3A8FB:  # BIM starts an image object capture
            in_image = True
            image_ipd = bytearray()
            image_obd = image_obp = None
            image_name = f.token_name
        elif f.sf_id == 0xD3A9FB and in_image:  # EIM completes it
            in_image = False
            obj = _finish_image_object(bytes(image_ipd), image_obd, image_obp)
            if obj is not None:
                if current is not None:  # inline: place on the page now
                    current.images.append(
                        ImageRef(
                            x=obj.x,
                            y=obj.y,
                            width=_scale(
                                obj.width, obj.upi, current.units_per_inch
                            ),
                            height=_scale(
                                obj.height, obj.upi, current.units_per_inch
                            ),
                            mime=obj.mime,
                            data=obj.blob,
                            bands=obj.bands,
                        )
                    )
                else:
                    for key in (image_name, resource_name):
                        if key:
                            image_resources.setdefault(key, obj)
                    loose_images.append(obj)
        elif (in_image or in_barcode or in_graphic) and f.sf_id == 0xD3A66B:  # OBD
            image_obd = f.data
        elif (in_image or in_barcode or in_graphic) and f.sf_id == 0xD3AC6B:  # OBP
            image_obp = f.data
        elif in_image and f.sf_id == 0xD3EEFB:  # IPD: IOCA segment bytes
            image_ipd += f.data
        elif f.sf_id == 0xD3A8EB:  # BBC starts a bar code object
            in_barcode = True
            barcode_bdd = b""
            barcode_bdas = []
            image_obd = image_obp = None
        elif in_barcode and f.sf_id == 0xD3A6EB:  # BDD: symbol descriptor
            barcode_bdd = f.data
        elif in_barcode and f.sf_id == 0xD3EEEB:  # BDA: one symbol each
            barcode_bdas.append(f.data)
        elif f.sf_id == 0xD3A9EB and in_barcode:  # EBC completes it
            in_barcode = False
            if current is not None:
                ox, oy = _parse_obp(image_obp) if image_obp else (0, 0)
                upi = current.units_per_inch
                for bda in barcode_bdas:
                    bar = parse_barcode(barcode_bdd, bda)
                    generated = barcode_png(bar) if bar else None
                    if generated is None:
                        continue
                    png, modules = generated
                    side = round(modules * bar.module_mils * upi / 1000)
                    current.images.append(
                        ImageRef(
                            x=ox + _scale(bar.x, bar.upi, upi),
                            y=oy + _scale(bar.y, bar.upi, upi),
                            width=side,
                            height=side,
                            mime="image/png",
                            data=png,
                            crisp=True,
                        )
                    )
        elif f.sf_id == 0xD3A8BB:  # BGR starts a graphics object
            in_graphic = True
            graphic_gdd = b""
            graphic_gads = []
            image_obd = image_obp = None
        elif in_graphic and f.sf_id == 0xD3A6BB:  # GDD: graphics descriptor
            graphic_gdd = f.data
        elif in_graphic and f.sf_id == 0xD3EEBB:  # GAD: drawing order stream
            graphic_gads.append(f.data)
        elif f.sf_id == 0xD3A9BB and in_graphic:  # EGR completes it
            in_graphic = False
            if current is not None and graphic_gdd:
                goca = draw_goca(graphic_gdd, b"".join(graphic_gads))
                if goca is not None:
                    ox, oy = _parse_obp(image_obp) if image_obp else (0, 0)
                    obd = _parse_obd(image_obd) if image_obd else (None, 0, 0)
                    upi = current.units_per_inch
                    ow = _scale(obd[1], obd[0] or upi, upi)
                    oh = _scale(obd[2], obd[0] or upi, upi)
                    current.graphics.append(
                        VectorGraphic(x=ox, y=oy, width=ow, height=oh, graphic=goca)
                    )
        elif f.sf_id == 0xD3AFC3 and current is not None:  # IOB places one
            image = _parse_iob(
                f.data, resources, image_resources, current.units_per_inch
            )
            if image is not None:
                current.images.append(image)
        elif f.sf_id == 0xD3ABD8:  # MPO: map overlay local id -> name
            mpo_map.update(_parse_mpo(f.data))
        elif f.sf_id == 0xD3AFD8 and current is not None:  # IPO: place overlay
            _include_overlay(current, overlays, mpo_map, f.data)
        elif f.sf_id == 0xD3A8DF:  # BMO: capture an overlay like a page
            current = Page()
            if pgd_default:
                current.width, current.height, current.units_per_inch = pgd_default
            state = _TextState(fonts, codepage, font_codepages, embedded_text_fonts)
            in_overlay = True
            overlay_name = (f.token_name or "").strip()
        elif f.sf_id == 0xD3A9DF:  # EMO: store the captured overlay by name
            if current is not None and in_overlay:
                _estimate_font_sizes(current, fonts)
                if overlay_name:
                    overlays[overlay_name] = current
            current, state, in_overlay, overlay_name = None, None, False, None
        elif f.sf_id == 0xD3A8AF:  # BPG
            current = Page()
            if pgd_default:
                current.width, current.height, current.units_per_inch = pgd_default
            state = _TextState(fonts, codepage, font_codepages, embedded_text_fonts)
        elif f.sf_id == 0xD3A9AF:  # EPG
            if current is not None:
                _estimate_font_sizes(current, fonts)
                pages.append(current)
            current, state = None, None
        elif f.sf_id == 0xD3ABC3:  # MDR maps fonts to SCFL local ids
            fonts.update(parse_mdr_fonts(f.data))
        elif f.sf_id in (0xD3AB8A, 0xD3B18A):  # MCF labels coded fonts
            fmt1 = f.sf_id == 0xD3B18A
            for local_id, cp in parse_mcf_codepages(f.data, format1=fmt1).items():
                if cp.codec:
                    font_codepages[local_id] = cp.codec
            # Pair each local id with its embedded code page + character set
            # so the run can be drawn in the file's own raster glyphs.
            for lid, (cp_name, cs_name) in mcf_font_resources(
                f.data, format1=fmt1
            ).items():
                cp_map = code_pages.get(cp_name or "")
                glyphs = char_set_glyphs.get(cs_name or "")
                outlines = char_set_outlines.get(cs_name or "")
                # External code page (not embedded) but the character set IS
                # embedded: reconstruct byte→GCGID from the codec via the
                # standard GCGID naming rules, so the file's own glyphs can
                # still be drawn instead of a substitute font.
                if not cp_map:
                    codec = font_codepages.get(lid)
                    if codec:
                        gset = set(glyphs or ()) | set(
                            outlines[0] if outlines else ())
                        bridged = bridge_code_page(codec, gset)
                        if bridged:
                            cp_map = bridged
                codec = font_codepages.get(lid)  # for the hidden text layer
                if cp_map and glyphs:
                    ref = max((g.height for g in glyphs.values()), default=1)
                    res, psize = char_set_metrics.get(cs_name or "", (0, 0.0))
                    embedded_text_fonts[lid] = _EmbeddedFont(
                        cp_map, glyphs, ref, codec=codec,
                        resolution=res, point_size=psize,
                    )
                elif cp_map and outlines and outlines[0]:
                    outline_glyphs, em = outlines
                    embedded_text_fonts[lid] = _EmbeddedFont(
                        cp_map, {}, 1,
                        outline_glyphs=outline_glyphs, units_per_em=em,
                        codec=codec,
                    )
                # Choose a substitute font: prefer the embedded char set's
                # typeface, else infer from the coded-font name (external
                # fonts). MDR, if present, already set a better one.
                sub = (_substitute_font(char_set_typefaces.get(cs_name or "", ""))
                       or _coded_font_substitute(cs_name or ""))
                # The external char-set name also encodes its point size, the
                # only size signal when the file embeds no font and has no MDR.
                size_pt = _coded_font_point_size(cs_name or "")
                if sub and lid not in fonts:
                    fonts[lid] = FontInfo(
                        family=sub[0], weight=sub[1], size_pt=size_pt)
                elif lid not in fonts and lid not in embedded_text_fonts:
                    # External font whose typeface we can't even infer from the
                    # coded-font name (e.g. C0EXxxxx): register it anyway so the
                    # run is still flagged, drawn in the default Arial.
                    fonts[lid] = FontInfo(size_pt=size_pt)
                # Fidelity: flag ids that will draw in a substitute font (i.e.
                # not via the file's own embedded glyphs) so the viewer can
                # explain the approximation. Skip ids drawn in real glyphs.
                info = fonts.get(lid)
                if info is not None and lid not in embedded_text_fonts:
                    label = _typeface_label(cs_name or "", char_set_typefaces)
                    fam = _FAMILY_LABEL.get(info.family, info.family)
                    cs_embedded = bool(
                        char_set_glyphs.get(cs_name or "")
                        or char_set_outlines.get(cs_name or "")
                    )
                    disp = (cs_name or "").strip() or "?"
                    if cs_embedded:
                        msg = (
                            f"Drawn in substitute font {fam}"
                            + (f" for {label}" if label else "")
                            + " — the embedded glyphs aren't used at this size."
                        )
                    else:
                        who = f"{label} (“{disp}”)" if label else f"“{disp}”"
                        msg = (
                            f"{who} is referenced but not embedded in this "
                            f"file — rendered with the system {fam}; "
                            f"glyph widths are approximated."
                        )
                    if not any(n.msg == msg for n in info.notes):
                        info.notes.append(FidelityNote("font", msg))
        elif f.sf_id == 0xD3A6AF and len(f.data) >= 12:  # PGD
            parsed = _parse_pgd(f.data)
            if current is not None:
                current.width, current.height, current.units_per_inch = parsed
            else:
                pgd_default = parsed
        elif f.sf_id == 0xD3EE9B:  # PTX
            if current is None:
                if implicit is None:
                    implicit = Page()
                    if pgd_default:
                        (
                            implicit.width,
                            implicit.height,
                            implicit.units_per_inch,
                        ) = pgd_default
                    implicit_state = _TextState(fonts, codepage, font_codepages, embedded_text_fonts)
                    implicit_state.wrap_width = implicit.width - 480
                    implicit_state.i = 240
                    implicit_state.b = 320
                target, target_state = implicit, implicit_state
            else:
                target, target_state = current, state
            target_state.field_offset = f.offset
            for cs in iter_control_sequences(f.data):
                target_state.apply(cs, target)

    if implicit is not None and (implicit.texts or implicit.rules):
        _estimate_font_sizes(implicit, fonts)
        pages.extend(_paginate_implicit(implicit))
    if not pages:
        # Font resource files (BFN...EFN, no document pages) get a
        # specimen sheet of their embedded raster glyphs.
        specimen = _font_specimen_pages(parse_fonts(fields))
        if specimen:
            return specimen
    if not pages and overlays and not any(f.sf_id == 0xD3A8A8 for f in fields):
        # Standalone overlay *resource* files (BMO...EMO with no enclosing
        # document — no BDT — and no IPO to place them) carry their own
        # geometry and content; present each as its own page, the way AFP
        # viewers open a bare overlay. An overlay merely defined-but-unused
        # inside a real document (which has a BDT) is left alone.
        for overlay in overlays.values():
            if overlay.texts or overlay.rules or overlay.images or overlay.graphics:
                pages.append(overlay)
    if not pages and loose_images:
        # Standalone object / resource-only files have no pages to
        # place these on; show each image object at its own extent.
        for obj in loose_images:
            page = Page(
                width=obj.width, height=obj.height, units_per_inch=obj.upi
            )
            page.images.append(
                ImageRef(
                    x=0, y=0, width=obj.width, height=obj.height,
                    mime=obj.mime, data=obj.blob, bands=obj.bands,
                )
            )
            pages.append(page)
    _annotate_image_notes(pages)
    return pages


def _parse_mpo(data: bytes) -> Dict[int, str]:
    """Map overlay local id → name from a Map Page Overlay (MPO) field.

    MPO is a series of repeating groups, each a 2-byte length then triplets:
    the Resource Local Identifier triplet (X'24') carries the local id (its
    last byte) and the Fully Qualified Name triplet (X'02') carries the
    overlay name (a FQN type/format byte pair then the EBCDIC name). The map
    lets an IPO reference an overlay by local id instead of by name.
    """
    out: Dict[int, str] = {}
    pos = 0
    while pos + 2 <= len(data):
        rg_len = int.from_bytes(data[pos : pos + 2], "big")
        if rg_len < 2 or pos + rg_len > len(data):
            break
        local_id: Optional[int] = None
        name: Optional[str] = None
        for tid, tdata in iter_triplets(data[pos + 2 : pos + rg_len]):
            if tid == 0x24 and tdata:  # Resource Local Identifier
                local_id = tdata[-1]
            elif tid == 0x02 and len(tdata) >= 3:  # Fully Qualified Name
                try:
                    name = tdata[2:].decode("cp500").strip()
                except UnicodeDecodeError:
                    name = None
        if local_id is not None and name:
            out[local_id] = name
        pos += rg_len
    return out


def _resolve_overlay(
    ref: bytes, overlays: Dict[str, Page], mpo_map: Dict[int, str]
) -> Optional[Page]:
    """Find the overlay an IPO references, by name or by MPO local id.

    The reference is normally an 8-EBCDIC-char name. When that doesn't match
    a captured overlay and the field is instead a single non-space, non-zero
    byte, it is treated as an MPO local id and resolved through ``mpo_map`` —
    but only ever to an overlay actually captured in the file, so a stray id
    can never fabricate content. (No corpus file references by id; the path
    is exercised by unit tests, like the TLE feature.)
    """
    try:
        name = ref[:8].decode("cp500").strip()
    except UnicodeDecodeError:
        name = ""
    if name and name in overlays:
        return overlays[name]
    nonblank = [b for b in ref[:8] if b not in (0x00, 0x40)]  # 0x40 = EBCDIC SP
    if len(nonblank) == 1:
        mapped = mpo_map.get(nonblank[0])
        if mapped and mapped in overlays:
            return overlays[mapped]
    return None


def _include_overlay(
    page: Page, overlays: Dict[str, Page], mpo_map: Dict[int, str], ipo: bytes
) -> None:
    """Composite a page overlay onto a page (IPO field).

    IPO layout: overlay reference (8 EBCDIC bytes — a name, or an MPO local
    id) then signed 3-byte X and Y offsets, in the including page's L-units.
    The overlay's text, rules, images and graphics are copied in, shifted by
    the offset and scaled if the overlay declared a different resolution.
    Overlay content is appended before the page's own body (IPO precedes it),
    so it renders underneath like a form or letterhead.
    """
    overlay = _resolve_overlay(ipo, overlays, mpo_map)
    if overlay is None:
        return
    ox = int.from_bytes(ipo[8:11], "big", signed=True) if len(ipo) >= 11 else 0
    oy = int.from_bytes(ipo[11:14], "big", signed=True) if len(ipo) >= 14 else 0
    src_upi = overlay.units_per_inch or page.units_per_inch
    dst_upi = page.units_per_inch

    def sx(v: int) -> int:
        return ox + _scale(v, src_upi, dst_upi)

    def sy(v: int) -> int:
        return oy + _scale(v, src_upi, dst_upi)

    def sz(v: int) -> int:
        return _scale(v, src_upi, dst_upi)

    for t in overlay.texts:
        page.texts.append(
            replace(t, x=sx(t.x), y=sy(t.y), font_size=sz(t.font_size))
        )
    for r in overlay.rules:
        page.rules.append(
            replace(
                r, x=sx(r.x), y=sy(r.y),
                length=sz(r.length), thickness=sz(r.thickness),
            )
        )
    for im in overlay.images:
        page.images.append(
            replace(
                im, x=sx(im.x), y=sy(im.y),
                width=sz(im.width), height=sz(im.height),
            )
        )
    for g in overlay.graphics:
        page.graphics.append(
            replace(
                g, x=sx(g.x), y=sy(g.y),
                width=sz(g.width), height=sz(g.height),
            )
        )


def _font_specimen_pages(fonts: List[Font]) -> List[Page]:
    """Lay out each embedded font as a labeled specimen sheet.

    Raster fonts get one page of decoded glyph bitmaps. Outline (Type 1 /
    CID) fonts, whose shapes we do not rasterize, instead get a metadata
    page listing the typeface, technology and per-character increments so
    the file is not rendered blank. The trivial space pattern is skipped.
    """
    upi = 1440
    page_w = 12240
    margin = 600
    title_size = 440  # ~22pt
    label_size = 170  # ~8.5pt
    glyph_h = 700
    cell_w = 1700
    cell_h = 1300
    grid_top = 980

    pages: List[Page] = []
    for font in fonts:
        glyphs = [g for g in font.glyphs if g.width >= 3 and g.height >= 3]
        if not glyphs:
            if font.is_outline and font.chars:
                pages.append(_outline_font_page(font))
            continue
        cols = max(1, (page_w - 2 * margin) // cell_w)
        rows = (len(glyphs) + cols - 1) // cols
        page = Page(
            width=page_w,
            height=grid_top + rows * cell_h + margin,
            units_per_inch=upi,
        )
        label = font.typeface or font.name or "embedded font"
        page.texts.append(
            TextRun(
                x=margin,
                y=margin,
                text=f"Embedded raster font: {label} — {len(glyphs)} glyphs",
                font_size=title_size,
                font_weight="bold",
            )
        )
        for i, g in enumerate(glyphs):
            col, row = i % cols, i // cols
            cell_x = margin + col * cell_w
            cell_y = grid_top + row * cell_h
            gw = round(glyph_h * g.width / g.height)
            gh = glyph_h
            max_w = cell_w - 200
            if gw > max_w:  # very wide glyph: fit to the cell width
                gh = round(gh * max_w / gw)
                gw = max_w
            page.images.append(
                ImageRef(
                    x=cell_x + (cell_w - gw) // 2,
                    y=cell_y + (glyph_h - gh),
                    width=gw,
                    height=gh,
                    mime="image/png",
                    data=g.png,
                )
            )
            page.texts.append(
                TextRun(
                    x=cell_x + 40,
                    y=cell_y + glyph_h + label_size + 60,
                    text=g.gcgid or "?",
                    color="#666666",
                    font_size=label_size,
                    font_family="Consolas",
                )
            )
        for t in page.texts:  # fixed grid layout: never width-fit
            t.fit = False
        pages.append(page)
    return pages


# Outline fonts have no bitmaps; the metadata page shows up to this many
# character metrics before summarizing the remainder.
_OUTLINE_CHARS_SHOWN = 256


def _outline_glyph_page(font: Font) -> Page:
    """Lay out an outline font's decoded glyph shapes as a specimen grid.

    Each glyph is drawn as an SVG path nested in a VectorGraphic, scaled
    from the font's design-unit em and sat on a common baseline, labeled by
    its glyph name (or GCGID). This is the font's true printed shape.
    """
    upi = 1440
    page_w = 12240
    margin = 600
    title_size = 440  # ~22pt
    sub_size = 220  # ~11pt
    label_size = 160  # ~8pt
    cell_w = 1500
    cell_h = 1500
    glyph_h = 1000  # drawn glyph height in L-units
    grid_top = 1640

    em = font.units_per_em or 1000
    ascent = round(0.80 * em)
    descent = round(0.24 * em)
    box_h = ascent + descent  # design-unit height of the drawn box
    gw = round(glyph_h * em / box_h)  # L-unit width of the em-square box

    drawable = [
        (cm, font.outline_glyphs[cm.gcgid])
        for cm in font.chars
        if cm.gcgid in font.outline_glyphs
        and font.outline_glyphs[cm.gcgid].segments
    ]
    cols = max(1, (page_w - 2 * margin) // cell_w)
    rows = (len(drawable) + cols - 1) // cols
    page = Page(
        width=page_w,
        height=grid_top + rows * cell_h + margin,
        units_per_inch=upi,
    )
    label = font.typeface or font.name or "embedded font"
    page.texts.append(
        TextRun(
            x=margin, y=margin,
            text=f"Embedded outline font: {label}",
            font_size=title_size, font_weight="bold", fit=False,
        )
    )
    page.texts.append(
        TextRun(
            x=margin, y=margin + sub_size + 160,
            text=f"{len(drawable)} glyphs · {font.tech_label} · "
                 f"{em}-unit em — actual embedded outlines",
            color="#666666", font_size=sub_size, fit=False,
        )
    )
    for i, (cm, glyph) in enumerate(drawable):
        col, row = i % cols, i // cols
        cell_x = margin + col * cell_w
        cell_y = grid_top + row * cell_h
        # Path in the box's own (y-down) space: baseline at y=ascent.
        d = glyph_to_path_d(glyph, scale=1, ox=0, oy=ascent)
        page.graphics.append(
            VectorGraphic(
                x=cell_x + (cell_w - gw) // 2,
                y=cell_y,
                width=gw,
                height=glyph_h,
                graphic=GocaGraphic(
                    svg=f'<path d="{d}" fill="#1a1a2a"/>',
                    gps_w=em,
                    gps_h=box_h,
                ),
            )
        )
        page.texts.append(
            TextRun(
                x=cell_x + 40,
                y=cell_y + glyph_h + label_size + 40,
                text=cm.name or cm.gcgid,
                color="#666666",
                font_size=label_size,
                font_family="Consolas",
                fit=False,
            )
        )
    return page


def _outline_font_page(font: Font) -> Page:
    """Build a specimen page for an outline font.

    When the Type 1 outlines were decoded, draw the actual glyph shapes in
    a labeled grid (what the font would print). Otherwise fall back to a
    metadata page listing the typeface, technology and per-character
    increments, so the file still surfaces its contents instead of
    rendering blank.
    """
    if any(g.segments for g in font.outline_glyphs.values()):
        return _outline_glyph_page(font)
    upi = 1440
    page_w = 12240
    margin = 600
    title_size = 440  # ~22pt
    sub_size = 220  # ~11pt
    label_size = 200  # ~10pt
    # Three columns when a Font Name Map gives readable glyph names (which
    # need the extra width); otherwise four columns of bare GCGIDs.
    named = any(c.name for c in font.chars)
    cols = 3 if named else 4
    cell_w = (page_w - 2 * margin) // cols
    row_h = 340
    grid_top = 1640

    chars = font.chars[:_OUTLINE_CHARS_SHOWN]
    remaining = len(font.chars) - len(chars)
    rows = (len(chars) + cols - 1) // cols
    extra = 1 if remaining else 0
    page = Page(
        width=page_w,
        height=grid_top + (rows + extra) * row_h + margin,
        units_per_inch=upi,
    )

    label = font.typeface or font.name or "embedded font"
    page.texts.append(
        TextRun(
            x=margin, y=margin,
            text=f"Embedded outline font: {label}",
            font_size=title_size, font_weight="bold",
        )
    )
    orient = (
        f" × {font.orientations} orientations" if font.orientations > 1 else ""
    )
    page.texts.append(
        TextRun(
            x=margin, y=margin + sub_size + 160,
            text=f"{font.tech_label} — {len(font.chars)} characters{orient}; "
                 f"glyph shapes not rasterized",
            color="#666666", font_size=sub_size,
        )
    )
    heading = (
        "GCGID · glyph name · inline increment (font design units):"
        if named
        else "GCGID + inline increment (font design units):"
    )
    page.texts.append(
        TextRun(
            x=margin, y=margin + 2 * (sub_size + 160),
            text=heading,
            color="#666666", font_size=sub_size,
        )
    )

    for i, cm in enumerate(chars):
        col, row = i % cols, i // cols
        gid = cm.gcgid or "?"
        cell = f"{gid:<8}  {cm.name}  {cm.char_increment}" if named \
            else f"{gid:<8}  {cm.char_increment}"
        page.texts.append(
            TextRun(
                x=margin + col * cell_w,
                y=grid_top + row * row_h,
                text=cell,
                color="#444444",
                font_size=label_size,
                font_family="Consolas",
            )
        )
    if remaining:
        page.texts.append(
            TextRun(
                x=margin,
                y=grid_top + rows * row_h + row_h // 2,
                text=f"... and {remaining} more characters",
                color="#666666",
                font_size=sub_size,
            )
        )
    for t in page.texts:  # fixed grid layout: never width-fit
        t.fit = False
    return page


def _paginate_implicit(page: Page) -> List[Page]:
    """Split an implicit page's flowed content into page-height chunks."""
    xs = [r.x for r in page.texts] + [r.x for r in page.rules]
    if xs:  # explicitly positioned content may still exceed the width
        page.width = max(page.width, max(xs) + 6 * DEFAULT_FONT_SIZE)
    usable = page.height - 320
    chunks: Dict[int, Page] = {}
    for kind in ("texts", "rules"):
        for item in getattr(page, kind):
            idx = item.y // usable
            chunk = chunks.setdefault(
                idx,
                Page(
                    width=page.width,
                    height=page.height,
                    units_per_inch=page.units_per_inch,
                ),
            )
            item.y -= idx * usable
            getattr(chunk, kind).append(item)
    ordered = [chunks[idx] for idx in sorted(chunks)]
    if ordered and page.truncated:
        ordered[-1].truncated = True
    return ordered
