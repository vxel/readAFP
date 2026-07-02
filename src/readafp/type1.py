"""Adobe Type 1 (PFB) outline font interpreter.

FOCA outline fonts (PatTech X'1E'/X'1F') embed a vendor font program in
their FNG pattern data. For Adobe Type 1 fonts that program is a PFB
stream: a cleartext header, an ``eexec``-encrypted binary block holding
the Private dict / Subrs / CharStrings, and a cleartext trailer.

This module reconstructs the program from the FNG bytes, decrypts it, and
interprets each glyph's Type 1 charstring into an outline path so the
font's true glyph shapes can be drawn (rather than substituted). The path
is emitted in the font's own design units (typically a 1000-unit em);
callers flip to SVG's y-down space and scale per point size.

References: Adobe Type 1 Font Format (the "black book"); charstring
operators and the eexec/charstring cipher in chapters 2 and 6.
"""

import logging
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Charstring/eexec cipher constants.
_C1 = 52845
_C2 = 22719
_EEXEC_R = 55665
_CHARSTRING_R = 4330

_MAX_SUBR_DEPTH = 30  # guard against pathological recursion
_MAX_SUBRS = 65536  # highest accepted subr index; real fonts use hundreds


# A path is a list of segments in glyph design units:
#   ("m", x, y) | ("l", x, y) | ("c", x1, y1, x2, y2, x3, y3) | ("z",)
Segment = tuple


@dataclass
class Glyph:
    """One decoded outline glyph."""

    advance: float  # inline advance width, in design units
    segments: List[Segment] = field(default_factory=list)


def _decrypt(cipher: bytes, r: int, skip: int) -> bytes:
    """Apply the Type 1 eexec/charstring cipher, dropping the lead bytes."""
    out = bytearray()
    for b in cipher:
        out.append(b ^ (r >> 8))
        r = ((b + r) * _C1 + _C2) & 0xFFFF
    return bytes(out[skip:])


def _pfb_payload(fng: bytes) -> bytes:
    """Reconstruct the raw Type 1 program from a PFB-segmented FNG blob.

    PFB segments are ``0x80 <type> <u32 little-endian length> <data>`` with
    type 1 = ASCII, 2 = binary, 3 = EOF. The ASCII and binary segments are
    concatenated in order to rebuild the cleartext-header + eexec-binary +
    cleartext-trailer program. If no marker is present the FNG is assumed
    to already be a raw program (returned from the ``%!`` header on).
    """
    start = fng.find(b"\x80")
    if start < 0 or start + 6 > len(fng):
        head = fng.find(b"%!")
        return fng[head:] if head >= 0 else fng
    parts: List[bytes] = []
    i = start
    while i + 6 <= len(fng) and fng[i] == 0x80:
        seg_type = fng[i + 1]
        if seg_type == 3:  # EOF
            break
        length = struct.unpack("<I", fng[i + 2 : i + 6])[0]
        parts.append(fng[i + 6 : i + 6 + length])
        i += 6 + length
    return b"".join(parts)


def _eexec_private(program: bytes) -> bytes:
    """Return the decrypted Private/CharStrings region after ``eexec``."""
    marker = program.find(b"eexec")
    if marker < 0:
        return b""
    pos = marker + 5
    # Skip the whitespace that follows the eexec token.
    while pos < len(program) and program[pos] in b" \t\r\n":
        pos += 1
    cipher = program[pos:]
    # Binary PFB hands us raw bytes; a raw stream may be ASCII-hex instead.
    sample = cipher[:4]
    if all(c in b"0123456789abcdefABCDEF \t\r\n" for c in sample):
        hexed = bytes(c for c in cipher if c in b"0123456789abcdefABCDEF")
        try:
            cipher = bytes.fromhex(hexed[: len(hexed) // 2 * 2].decode("ascii"))
        except ValueError:
            pass
    return _decrypt(cipher, _EEXEC_R, 4)


# Adobe StandardEncoding — code point -> glyph name, needed to resolve the
# base and accent of a `seac` composite (e.g. acircumflex = a + circumflex).
_STD_ENCODING: Dict[int, str] = {
    32: "space", 33: "exclam", 34: "quotedbl", 35: "numbersign",
    36: "dollar", 37: "percent", 38: "ampersand", 39: "quoteright",
    40: "parenleft", 41: "parenright", 42: "asterisk", 43: "plus",
    44: "comma", 45: "hyphen", 46: "period", 47: "slash",
    48: "zero", 49: "one", 50: "two", 51: "three", 52: "four",
    53: "five", 54: "six", 55: "seven", 56: "eight", 57: "nine",
    58: "colon", 59: "semicolon", 60: "less", 61: "equal",
    62: "greater", 63: "question", 64: "at",
    65: "A", 66: "B", 67: "C", 68: "D", 69: "E", 70: "F", 71: "G",
    72: "H", 73: "I", 74: "J", 75: "K", 76: "L", 77: "M", 78: "N",
    79: "O", 80: "P", 81: "Q", 82: "R", 83: "S", 84: "T", 85: "U",
    86: "V", 87: "W", 88: "X", 89: "Y", 90: "Z",
    91: "bracketleft", 92: "backslash", 93: "bracketright",
    94: "asciicircum", 95: "underscore", 96: "quoteleft",
    97: "a", 98: "b", 99: "c", 100: "d", 101: "e", 102: "f", 103: "g",
    104: "h", 105: "i", 106: "j", 107: "k", 108: "l", 109: "m",
    110: "n", 111: "o", 112: "p", 113: "q", 114: "r", 115: "s",
    116: "t", 117: "u", 118: "v", 119: "w", 120: "x", 121: "y",
    122: "z", 123: "braceleft", 124: "bar", 125: "braceright",
    126: "asciitilde", 161: "exclamdown", 162: "cent", 163: "sterling",
    164: "fraction", 165: "yen", 166: "florin", 167: "section",
    168: "currency", 169: "quotesingle", 170: "quotedblleft",
    171: "guillemotleft", 172: "guilsinglleft", 173: "guilsinglright",
    174: "fi", 175: "fl", 177: "endash", 178: "dagger", 179: "daggerdbl",
    180: "periodcentered", 182: "paragraph", 183: "bullet",
    184: "quotesinglbase", 185: "quotedblbase", 186: "quotedblright",
    187: "guillemotright", 188: "ellipsis", 189: "perthousand",
    191: "questiondown", 193: "grave", 194: "acute", 195: "circumflex",
    196: "tilde", 197: "macron", 198: "breve", 199: "dotaccent",
    200: "dieresis", 202: "ring", 203: "cedilla", 205: "hungarumlaut",
    206: "ogonek", 207: "caron", 208: "emdash", 225: "AE",
    227: "ordfeminine", 232: "Lslash", 233: "Oslash", 234: "OE",
    235: "ordmasculine", 241: "ae", 245: "dotlessi", 248: "lslash",
    249: "oslash", 250: "oe", 251: "germandbls",
}


class Type1Font:
    """A parsed Adobe Type 1 font program decoded from FOCA FNG bytes."""

    def __init__(self, fng: bytes) -> None:
        program = _pfb_payload(fng)
        self.font_matrix = _parse_font_matrix(program)
        self.units_per_em = (
            round(1 / self.font_matrix[0]) if self.font_matrix[0] else 1000
        )
        private = _eexec_private(program)
        m = re.search(rb"/lenIV\s+(\d+)", private)
        self._len_iv = int(m.group(1)) if m else 4
        self._subrs = _parse_subrs(private, self._len_iv)
        self._charstrings = _parse_charstrings(private, self._len_iv)

    @property
    def glyph_names(self) -> List[str]:
        return list(self._charstrings)

    def has_glyph(self, name: str) -> bool:
        return name in self._charstrings

    def glyph(self, name: str) -> Optional[Glyph]:
        """Interpret a glyph's charstring into an outline, or None."""
        cs = self._charstrings.get(name)
        if cs is None:
            return None
        interp = _Interpreter(self._subrs, self._charstrings)
        try:
            interp.run(cs)
        except (IndexError, ValueError, RecursionError, TypeError,
                struct.error) as exc:
            # struct.error (truncated operand) is NOT a ValueError subclass;
            # TypeError covers short-stack splats into _curveto/_seac.
            logger.warning("Type 1 charstring %r failed: %s", name, exc)
            return None
        return Glyph(advance=interp.width, segments=interp.segments)


def _parse_font_matrix(program: bytes) -> Tuple[float, ...]:
    """Read /FontMatrix; default to a 1000-unit em if absent."""
    m = re.search(rb"/FontMatrix\s*\[([^\]]+)\]", program)
    if m:
        try:
            vals = tuple(float(x) for x in m.group(1).split())
            if len(vals) == 6:
                return vals
        except ValueError:
            pass
    return (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)


def _binary_entries(blob: bytes, len_iv: int) -> List[Tuple[bytes, bytes]]:
    """Parse ``/name len RD <bytes> ND`` style binary entries.

    Handles the standard ``RD``/``-|`` read operators. Returns a list of
    (name, decrypted-charstring) preserving file order.
    """
    out: List[Tuple[bytes, bytes]] = []
    for m in re.finditer(rb"/([^ \t\r\n/{}\[\]()]+)[ \t]+(\d+)[ \t]+(RD|-\|)[ ]",
                         blob):
        length = int(m.group(2))
        data_start = m.end()
        raw = blob[data_start : data_start + length]
        out.append((m.group(1), _decrypt(raw, _CHARSTRING_R, len_iv)))
    return out


def _parse_charstrings(private: bytes, len_iv: int) -> Dict[str, bytes]:
    """Build {glyph name: decrypted charstring} from the CharStrings dict."""
    idx = private.find(b"/CharStrings")
    region = private[idx:] if idx >= 0 else private
    chars: Dict[str, bytes] = {}
    for name, data in _binary_entries(region, len_iv):
        key = name.decode("latin-1")
        if key not in chars:
            chars[key] = data
    return chars


def _parse_subrs(private: bytes, len_iv: int) -> List[bytes]:
    """Build the Subrs array (indexed local subroutines)."""
    idx = private.find(b"/Subrs")
    if idx < 0:
        return []
    # End the scan at CharStrings so glyph entries are not mistaken for subrs.
    end = private.find(b"/CharStrings")
    region = private[idx : end if end > idx else len(private)]
    subrs: Dict[int, bytes] = {}
    for m in re.finditer(rb"dup[ \t]+(\d+)[ \t]+(\d+)[ \t]+(RD|-\|)[ ]", region):
        num = int(m.group(1))
        if num >= _MAX_SUBRS:  # hostile index would densify to a huge list
            logger.warning("ignoring Type 1 subr %d (limit %d)", num, _MAX_SUBRS)
            continue
        length = int(m.group(2))
        start = m.end()
        subrs[num] = _decrypt(region[start : start + length], _CHARSTRING_R, len_iv)
    if not subrs:
        return []
    return [subrs.get(i, b"") for i in range(max(subrs) + 1)]


class _Interpreter:
    """Executes one Type 1 charstring, accumulating an outline path."""

    def __init__(self, subrs: List[bytes], charstrings: Dict[str, bytes]) -> None:
        self.subrs = subrs
        self.charstrings = charstrings
        self.stack: List[float] = []
        self.ps_stack: List[float] = []  # results of callothersubr, read by pop
        self.segments: List[Segment] = []
        self.x = 0.0
        self.y = 0.0
        self.width = 0.0
        self.sbx = 0.0
        self._open = False
        self._flex = False
        self._flex_pts: List[Tuple[float, float]] = []
        self._done = False

    # -- path helpers --------------------------------------------------
    def _moveto(self, dx: float, dy: float) -> None:
        self.x += dx
        self.y += dy
        if self._flex:
            self._flex_pts.append((self.x, self.y))
            return
        if self._open:
            self.segments.append(("z",))
        self.segments.append(("m", self.x, self.y))
        self._open = True

    def _lineto(self, dx: float, dy: float) -> None:
        self.x += dx
        self.y += dy
        self.segments.append(("l", self.x, self.y))

    def _curveto(self, dx1, dy1, dx2, dy2, dx3, dy3) -> None:
        x1, y1 = self.x + dx1, self.y + dy1
        x2, y2 = x1 + dx2, y1 + dy2
        x3, y3 = x2 + dx3, y2 + dy3
        self.segments.append(("c", x1, y1, x2, y2, x3, y3))
        self.x, self.y = x3, y3

    def _close(self) -> None:
        if self._open:
            self.segments.append(("z",))
            self._open = False

    # -- main loop -----------------------------------------------------
    def run(self, cs: bytes, depth: int = 0) -> None:
        if depth > _MAX_SUBR_DEPTH:
            raise RecursionError("charstring subr depth exceeded")
        i = 0
        n = len(cs)
        while i < n and not self._done:
            b = cs[i]
            i += 1
            if b >= 32:
                if b <= 246:
                    self.stack.append(b - 139)
                elif b <= 250:
                    self.stack.append((b - 247) * 256 + cs[i] + 108)
                    i += 1
                elif b <= 254:
                    self.stack.append(-(b - 251) * 256 - cs[i] - 108)
                    i += 1
                else:  # 255: 32-bit signed integer
                    self.stack.append(
                        struct.unpack(">i", cs[i : i + 4])[0]
                    )
                    i += 4
                continue
            i = self._operator(b, cs, i, depth)

    def _operator(self, b: int, cs: bytes, i: int, depth: int) -> int:
        s = self.stack
        if b == 13:  # hsbw: sbx wx
            self.sbx = s[0] if s else 0.0
            self.width = s[1] if len(s) > 1 else 0.0
            self.x, self.y = self.sbx, 0.0
            s.clear()
        elif b == 21:  # rmoveto
            self._moveto(s[-2], s[-1]) if len(s) >= 2 else self._moveto(0, 0)
            s.clear()
        elif b == 22:  # hmoveto
            self._moveto(s[-1] if s else 0, 0)
            s.clear()
        elif b == 4:  # vmoveto
            self._moveto(0, s[-1] if s else 0)
            s.clear()
        elif b == 5:  # rlineto
            self._lineto(s[-2], s[-1])
            s.clear()
        elif b == 6:  # hlineto
            self._lineto(s[-1], 0)
            s.clear()
        elif b == 7:  # vlineto
            self._lineto(0, s[-1])
            s.clear()
        elif b == 8:  # rrcurveto
            self._curveto(*s[-6:])
            s.clear()
        elif b == 30:  # vhcurveto: dy1 dx2 dy2 dx3
            self._curveto(0, s[0], s[1], s[2], s[3], 0)
            s.clear()
        elif b == 31:  # hvcurveto: dx1 dx2 dy2 dy3
            self._curveto(s[0], 0, s[1], s[2], 0, s[3])
            s.clear()
        elif b == 9:  # closepath
            self._close()
            s.clear()
        elif b == 1 or b == 3:  # hstem / vstem
            s.clear()
        elif b == 10:  # callsubr
            idx = int(s.pop()) if s else -1
            if 0 <= idx < len(self.subrs):
                self.run(self.subrs[idx], depth + 1)
        elif b == 11:  # return
            return i
        elif b == 14:  # endchar
            self._close()
            self._done = True
        elif b == 12:  # escape: two-byte operator
            b2 = cs[i]
            i += 1
            self._escape(b2)
        else:
            s.clear()
        return i

    def _escape(self, b2: int) -> None:
        s = self.stack
        if b2 == 6:  # seac: asb adx ady bchar achar
            self._seac(*s[-5:])
            s.clear()
        elif b2 == 7:  # sbw
            self.sbx = s[0] if s else 0.0
            self.width = s[2] if len(s) > 2 else 0.0
            self.x, self.y = self.sbx, (s[1] if len(s) > 1 else 0.0)
            s.clear()
        elif b2 == 12:  # div
            a = s.pop()
            b = s.pop()
            s.append(b / a if a else 0.0)
        elif b2 == 16:  # callothersubr
            self._callothersubr()
        elif b2 == 17:  # pop
            s.append(self.ps_stack.pop() if self.ps_stack else 0.0)
        elif b2 == 33:  # setcurrentpoint
            if len(s) >= 2:
                self.x, self.y = s[-2], s[-1]
            s.clear()
        else:  # dotsection / vstem3 / hstem3 / unknown
            s.clear()

    def _callothersubr(self) -> None:
        s = self.stack
        othersubr = int(s.pop()) if s else 0
        count = int(s.pop()) if s else 0
        args = [s.pop() for _ in range(count)][::-1] if count else []
        if othersubr == 1:  # start flex
            self._flex = True
            self._flex_pts = []
        elif othersubr == 2:  # flex point collected via rmoveto
            pass
        elif othersubr == 0:  # end flex -> two curves through collected points
            self._flex = False
            self._emit_flex()
            # Leave the end point for the two following pops.
            end_y = args[2] if len(args) > 2 else self.y
            end_x = args[1] if len(args) > 1 else self.x
            self.ps_stack = [end_y, end_x]
        elif othersubr == 3:  # hint replacement -> hand the subr# back to pop
            self.ps_stack = [args[0] if args else 3]
        else:  # unknown: make args available to subsequent pops
            self.ps_stack = list(args)

    def _emit_flex(self) -> None:
        """Replace the 7 collected flex points with two cubic curves."""
        p = self._flex_pts
        if len(p) < 7:
            return
        # p[0] is the reference point; p[1..3] and p[4..6] are the curves.
        self.segments.append(
            ("c", p[1][0], p[1][1], p[2][0], p[2][1], p[3][0], p[3][1])
        )
        self.segments.append(
            ("c", p[4][0], p[4][1], p[5][0], p[5][1], p[6][0], p[6][1])
        )
        self.x, self.y = p[6]

    def _seac(self, asb, adx, ady, bchar, achar) -> None:
        """Compose an accented glyph from a base and an accent character."""
        base = _STD_ENCODING.get(int(bchar))
        accent = _STD_ENCODING.get(int(achar))
        if base and base in self.charstrings:
            sub = _Interpreter(self.subrs, self.charstrings)
            sub.run(self.charstrings[base])
            self.segments.extend(sub.segments)
            self.width = sub.width or self.width
        if accent and accent in self.charstrings:
            sub = _Interpreter(self.subrs, self.charstrings)
            sub.run(self.charstrings[accent])
            ox = self.sbx - asb + adx
            oy = ady
            for seg in sub.segments:
                self.segments.append(_shift(seg, ox, oy))
        self._open = False
        self._done = True


def _shift(seg: Segment, dx: float, dy: float) -> Segment:
    """Translate a path segment by (dx, dy)."""
    if seg[0] == "z":
        return seg
    if seg[0] in ("m", "l"):
        return (seg[0], seg[1] + dx, seg[2] + dy)
    return (
        "c", seg[1] + dx, seg[2] + dy, seg[3] + dx, seg[4] + dy,
        seg[5] + dx, seg[6] + dy,
    )


def glyph_to_path_d(glyph: Glyph, scale: float, ox: float, oy: float) -> str:
    """Render a glyph's segments as an SVG path, flipping to y-down space.

    ``scale`` maps design units to the target size; (ox, oy) places the
    glyph origin (baseline) in the target coordinate system.
    """
    parts: List[str] = []
    for seg in glyph.segments:
        if seg[0] == "m":
            parts.append(f"M{ox + seg[1] * scale:.1f} {oy - seg[2] * scale:.1f}")
        elif seg[0] == "l":
            parts.append(f"L{ox + seg[1] * scale:.1f} {oy - seg[2] * scale:.1f}")
        elif seg[0] == "c":
            parts.append(
                f"C{ox + seg[1] * scale:.1f} {oy - seg[2] * scale:.1f} "
                f"{ox + seg[3] * scale:.1f} {oy - seg[4] * scale:.1f} "
                f"{ox + seg[5] * scale:.1f} {oy - seg[6] * scale:.1f}"
            )
        elif seg[0] == "z":
            parts.append("Z")
    return "".join(parts)
