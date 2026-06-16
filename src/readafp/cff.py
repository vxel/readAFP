"""CFF (Compact Font Format) / Type 2 charstring outline interpreter.

FOCA outline fonts (PatTech X'1E'/X'1F') embed a vendor font program in
their FNG pattern data. When that program is CFF — either a bare ``CFF``
table or the ``CFF`` table of an ``OTTO`` OpenType wrapper — its glyphs are
described by *Type 2* charstrings, a more compact relative of the Type 1
charstrings handled in :mod:`readafp.type1`. CID-keyed (Type 0) fonts are
CFF with an ``ROS`` operator, per-glyph Private dicts selected through an
FDArray / FDSelect, and a charset that maps glyph index to CID.

This module parses the CFF data structures and interprets each glyph's
Type 2 charstring into an outline path, reusing :class:`readafp.type1.Glyph`
and :func:`readafp.type1.glyph_to_path_d` so the renderer can draw CFF and
Type 1 glyphs identically. Runtime depends only on the standard library.

References: Adobe Technical Note #5176 (The Compact Font Format
Specification) and #5177 (The Type 2 Charstring Format).
"""

import logging
import struct
from typing import Dict, List, Optional, Tuple

from readafp.type1 import Glyph, Segment

logger = logging.getLogger(__name__)

_MAX_SUBR_DEPTH = 60  # Type 2 nests deeper than Type 1; guard recursion.

# The 391 standard strings (SID 0..390). Only the entries we need to name
# glyphs are spelled out; the rest are filled with placeholders so SID
# arithmetic against the String INDEX stays correct.
_STD_STRINGS = [
    ".notdef", "space", "exclam", "quotedbl", "numbersign", "dollar",
    "percent", "ampersand", "quoteright", "parenleft", "parenright",
    "asterisk", "plus", "comma", "hyphen", "period", "slash", "zero",
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "colon", "semicolon", "less", "equal", "greater", "question",
    "at", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
    "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    "bracketleft", "backslash", "bracketright", "asciicircum",
    "underscore", "quoteleft", "a", "b", "c", "d", "e", "f", "g", "h",
    "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v",
    "w", "x", "y", "z", "braceleft", "bar", "braceright", "asciitilde",
    "exclamdown", "cent", "sterling", "fraction", "yen", "florin",
    "section", "currency", "quotesingle", "quotedblleft", "guillemotleft",
    "guilsinglleft", "guilsinglright", "fi", "fl", "endash", "dagger",
    "daggerdbl", "periodcentered", "paragraph", "bullet", "quotesinglbase",
    "quotedblbase", "quotedblright", "guillemotright", "ellipsis",
    "perthousand", "questiondown", "grave", "acute", "circumflex", "tilde",
    "macron", "breve", "dotaccent", "dieresis", "ring", "cedilla",
    "hungarumlaut", "ogonek", "caron", "emdash", "AE", "ordfeminine",
    "Lslash", "Oslash", "OE", "ordmasculine", "ae", "dotlessi", "lslash",
    "oslash", "oe", "germandbls",
]
# Pad to the full 391 standard SIDs; named lookups beyond the list above
# fall back to "sidN", which is enough to key glyphs.
_STD_STRINGS += [f"sid{i}" for i in range(len(_STD_STRINGS), 391)]


def _read_card16(data: bytes, pos: int) -> int:
    return struct.unpack(">H", data[pos : pos + 2])[0]


def _read_offset(data: bytes, pos: int, size: int) -> int:
    """Read a big-endian unsigned integer of ``size`` (1..4) bytes."""
    val = 0
    for k in range(size):
        val = (val << 8) | data[pos + k]
    return val


def _parse_index(data: bytes, pos: int) -> Tuple[List[bytes], int]:
    """Parse a CFF INDEX at ``pos``; return (objects, position-after-index).

    An INDEX is a count (Card16), an offSize byte, ``count+1`` offsets each
    ``offSize`` bytes (1-based, relative to the byte before the data), then
    the object data. A zero count is just the 2-byte header.
    """
    count = _read_card16(data, pos)
    pos += 2
    if count == 0:
        return [], pos
    off_size = data[pos]
    pos += 1
    offsets = [
        _read_offset(data, pos + k * off_size, off_size)
        for k in range(count + 1)
    ]
    pos += (count + 1) * off_size
    base = pos - 1  # offsets are 1-based from the byte before the data
    objs = [data[base + offsets[k] : base + offsets[k + 1]] for k in range(count)]
    return objs, base + offsets[-1]


def _parse_dict(data: bytes) -> Dict[int, List[float]]:
    """Parse a CFF DICT into {operator: operands}.

    Operators 0..21 are single-byte; operator 12 is an escape forming the
    two-byte operator ``1200 + next``. Operands are integers (various
    encodings) or real numbers (operand 30, packed BCD nibbles).
    """
    out: Dict[int, List[float]] = {}
    operands: List[float] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b <= 21:  # operator
            op = b
            i += 1
            if op == 12:
                op = 1200 + data[i]
                i += 1
            out[op] = operands
            operands = []
        elif b == 28:
            operands.append(struct.unpack(">h", data[i + 1 : i + 3])[0])
            i += 3
        elif b == 29:
            operands.append(struct.unpack(">i", data[i + 1 : i + 5])[0])
            i += 5
        elif b == 30:  # real number, nibble-encoded
            val, i = _parse_real(data, i + 1)
            operands.append(val)
        elif 32 <= b <= 246:
            operands.append(b - 139)
            i += 1
        elif 247 <= b <= 250:
            operands.append((b - 247) * 256 + data[i + 1] + 108)
            i += 2
        elif 251 <= b <= 254:
            operands.append(-(b - 251) * 256 - data[i + 1] - 108)
            i += 2
        else:  # 22..27, 31, 255 are reserved in DICTs
            i += 1
    return out


_NIBBLE = {
    0x0: "0", 0x1: "1", 0x2: "2", 0x3: "3", 0x4: "4", 0x5: "5", 0x6: "6",
    0x7: "7", 0x8: "8", 0x9: "9", 0xA: ".", 0xB: "E", 0xC: "E-",
    0xE: "-",
}


def _parse_real(data: bytes, pos: int) -> Tuple[float, int]:
    """Decode an operand-30 real number; return (value, next-position)."""
    s = ""
    while pos < len(data):
        b = data[pos]
        pos += 1
        for nib in (b >> 4, b & 0xF):
            if nib == 0xF:  # end marker
                try:
                    return float(s) if s else 0.0, pos
                except ValueError:
                    return 0.0, pos
            s += _NIBBLE.get(nib, "")
    return (float(s) if s else 0.0), pos


def _subr_bias(count: int) -> int:
    """Type 2 subroutine index bias from the subr count."""
    if count < 1240:
        return 107
    if count < 33900:
        return 1131
    return 32768


def _parse_charset(
    data: bytes, pos: int, nglyphs: int
) -> List[int]:
    """Parse the charset; return SID (or CID) per glyph index.

    Glyph 0 (.notdef) is implicit and always SID 0. Formats 0, 1 and 2 are
    supported. For CID fonts these values are CIDs rather than string SIDs.
    """
    sids = [0]
    fmt = data[pos]
    pos += 1
    if fmt == 0:
        for _ in range(nglyphs - 1):
            sids.append(_read_card16(data, pos))
            pos += 2
    elif fmt in (1, 2):
        while len(sids) < nglyphs:
            first = _read_card16(data, pos)
            pos += 2
            if fmt == 1:
                left = data[pos]
                pos += 1
            else:
                left = _read_card16(data, pos)
                pos += 2
            for k in range(left + 1):
                if len(sids) >= nglyphs:
                    break
                sids.append(first + k)
    return sids


def _parse_fdselect(data: bytes, pos: int, nglyphs: int) -> List[int]:
    """Parse FDSelect; return the FD (font-dict) index per glyph."""
    fmt = data[pos]
    pos += 1
    fds = [0] * nglyphs
    if fmt == 0:
        for g in range(nglyphs):
            fds[g] = data[pos + g]
    elif fmt == 3:
        nranges = _read_card16(data, pos)
        pos += 2
        ranges = []
        for _ in range(nranges):
            first = _read_card16(data, pos)
            fd = data[pos + 2]
            ranges.append((first, fd))
            pos += 3
        sentinel = _read_card16(data, pos)
        ranges.append((sentinel, 0))
        for k in range(nranges):
            first, fd = ranges[k]
            nxt = ranges[k + 1][0]
            for g in range(first, min(nxt, nglyphs)):
                fds[g] = fd
    return fds


class _Private:
    """A resolved Private DICT: width defaults and local subroutines."""

    def __init__(self, default_w: float, nominal_w: float,
                 local_subrs: List[bytes]) -> None:
        self.default_width = default_w
        self.nominal_width = nominal_w
        self.local_subrs = local_subrs
        self.local_bias = _subr_bias(len(local_subrs))


class CFFFont:
    """A parsed CFF font program decoded from raw CFF (or OTTO) bytes."""

    def __init__(self, data: bytes) -> None:
        data = _extract_cff(data)
        self._data = data
        hdr_size = data[2]
        pos = hdr_size
        _names, pos = _parse_index(data, pos)
        top_dicts, pos = _parse_index(data, pos)
        strings, pos = _parse_index(data, pos)
        self._strings = strings
        self._gsubrs, pos = _parse_index(data, pos)
        self._gbias = _subr_bias(len(self._gsubrs))

        top = _parse_dict(top_dicts[0]) if top_dicts else {}
        self.is_cid = 1230 in top  # ROS operator marks a CID-keyed font

        fm = top.get(1207)
        self.font_matrix = tuple(fm) if fm and len(fm) == 6 else (
            0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
        self.units_per_em = (
            round(1 / self.font_matrix[0]) if self.font_matrix[0] else 1000)

        cs_off = int(top[17][0]) if 17 in top else 0
        self._charstrings, _ = _parse_index(data, cs_off)
        nglyphs = len(self._charstrings)

        # Per-glyph Private dicts: a single one for plain CFF, or one per
        # FDArray entry selected by FDSelect for a CID-keyed font.
        if self.is_cid:
            self._privates, self._fdselect = self._parse_cid_privates(
                top, nglyphs)
        else:
            self._privates = [self._parse_private(top.get(18))]
            self._fdselect = [0] * nglyphs

        charset_off = int(top[15][0]) if 15 in top else 0
        self._charset = self._build_charset(charset_off, nglyphs)
        self._name_to_gid = {
            name: gid for gid, name in enumerate(self._charset)
        }

    # -- charset / naming ----------------------------------------------
    def _build_charset(self, off: int, nglyphs: int) -> List[str]:
        """Return a glyph name per glyph index.

        For CID fonts the charset holds CIDs; names become ``cidNNNNN``.
        Predefined charsets (offsets 0/1/2) only define ISOAdobe for
        offset 0 here, which suffices for the standard glyph complement.
        """
        if off in (0, 1, 2):
            sids = list(range(nglyphs))
        else:
            sids = _parse_charset(self._data, off, nglyphs)
        if self.is_cid:
            return [f"cid{sid:05d}" for sid in sids]
        return [self._sid_name(sid) for sid in sids]

    def _sid_name(self, sid: int) -> str:
        if sid < len(_STD_STRINGS):
            return _STD_STRINGS[sid]
        idx = sid - len(_STD_STRINGS)
        if 0 <= idx < len(self._strings):
            try:
                return self._strings[idx].decode("latin-1")
            except UnicodeDecodeError:
                return f"sid{sid}"
        return f"sid{sid}"

    # -- private dicts --------------------------------------------------
    def _parse_private(self, spec: Optional[List[float]]) -> _Private:
        if not spec or len(spec) < 2:
            return _Private(0.0, 0.0, [])
        size, offset = int(spec[0]), int(spec[1])
        pdict = _parse_dict(self._data[offset : offset + size])
        default_w = pdict.get(20, [0.0])[0]
        nominal_w = pdict.get(21, [0.0])[0]
        local_subrs: List[bytes] = []
        if 19 in pdict:  # Subrs offset is relative to the Private DICT start
            subr_off = offset + int(pdict[19][0])
            local_subrs, _ = _parse_index(self._data, subr_off)
        return _Private(default_w, nominal_w, local_subrs)

    def _parse_cid_privates(
        self, top: Dict[int, List[float]], nglyphs: int
    ) -> Tuple[List[_Private], List[int]]:
        fdarray_off = int(top[1236][0]) if 1236 in top else 0
        fdselect_off = int(top[1237][0]) if 1237 in top else 0
        fd_dicts, _ = _parse_index(self._data, fdarray_off)
        privates = [
            self._parse_private(_parse_dict(fd).get(18)) for fd in fd_dicts
        ] or [_Private(0.0, 0.0, [])]
        fdselect = (
            _parse_fdselect(self._data, fdselect_off, nglyphs)
            if fdselect_off else [0] * nglyphs
        )
        return privates, fdselect

    # -- public API (mirrors type1.Type1Font) --------------------------
    @property
    def glyph_names(self) -> List[str]:
        return list(self._charset)

    def has_glyph(self, name: str) -> bool:
        return name in self._name_to_gid

    def glyph(self, name: str) -> Optional[Glyph]:
        gid = self._name_to_gid.get(name)
        if gid is None:
            return None
        return self.glyph_by_gid(gid)

    def glyph_by_gid(self, gid: int) -> Optional[Glyph]:
        """Interpret glyph ``gid``'s Type 2 charstring into an outline."""
        if not 0 <= gid < len(self._charstrings):
            return None
        priv = self._privates[self._fdselect[gid]]
        interp = _Interpreter(self._gsubrs, self._gbias, priv)
        try:
            interp.run(self._charstrings[gid])
            interp.finish()
        except (IndexError, ValueError, RecursionError) as exc:
            logger.warning("Type 2 charstring gid %d failed: %s", gid, exc)
            return None
        return Glyph(advance=interp.width, segments=interp.segments)


def _extract_cff(data: bytes) -> bytes:
    """Return the raw CFF bytes, unwrapping an OTTO OpenType container.

    A bare CFF starts with version (1, 0). An OpenType/CFF font starts with
    the ``OTTO`` tag and a table directory; the ``CFF `` table is sliced out
    by its directory entry.
    """
    if data[:4] == b"OTTO":
        num_tables = struct.unpack(">H", data[4:6])[0]
        pos = 12
        for _ in range(num_tables):
            tag = data[pos : pos + 4]
            offset = struct.unpack(">I", data[pos + 8 : pos + 12])[0]
            length = struct.unpack(">I", data[pos + 12 : pos + 16])[0]
            if tag == b"CFF ":
                return data[offset : offset + length]
            pos += 16
        raise ValueError("OTTO container has no CFF table")
    return data


class _Interpreter:
    """Executes one Type 2 charstring, accumulating an outline path.

    Type 2 differs from Type 1: numbers can be 16.16 fixed point, the y/x
    curve operators take alternating-axis run lengths, hints carry an
    implicit mask, subroutine indices are biased, and the glyph advance
    width is an optional leading operand on the first stack-clearing
    operator (added to the Private dict's nominalWidthX).
    """

    def __init__(self, gsubrs: List[bytes], gbias: int,
                 priv: _Private) -> None:
        self.gsubrs = gsubrs
        self.gbias = gbias
        self.subrs = priv.local_subrs
        self.lbias = priv.local_bias
        self.nominal_width = priv.nominal_width
        self.width = priv.default_width
        self.stack: List[float] = []
        self.segments: List[Segment] = []
        self.x = 0.0
        self.y = 0.0
        self.nstems = 0
        self._have_width = False
        self._open = False
        self._done = False

    # -- path helpers --------------------------------------------------
    def _moveto(self, dx: float, dy: float) -> None:
        self.x += dx
        self.y += dy
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

    def finish(self) -> None:
        if self._open:
            self.segments.append(("z",))
            self._open = False

    # -- width handling ------------------------------------------------
    def _take_width(self, even: bool) -> None:
        """Consume the optional leading width operand.

        ``even`` is the expected parity of the operand count *without* a
        width; if the actual count has the opposite parity the extra
        leading operand is the width delta from nominalWidthX.
        """
        if self._have_width:
            return
        has_extra = (len(self.stack) % 2 == 1) if even else (len(self.stack) > 1)
        if has_extra:
            self.width = self.nominal_width + self.stack.pop(0)
        self._have_width = True

    # -- main loop -----------------------------------------------------
    def run(self, cs: bytes, depth: int = 0) -> None:
        if depth > _MAX_SUBR_DEPTH:
            raise RecursionError("charstring subr depth exceeded")
        i = 0
        n = len(cs)
        while i < n and not self._done:
            b = cs[i]
            i += 1
            if b >= 32 or b == 28:
                i = self._operand(b, cs, i)
                continue
            stop = self._operator(b, cs, i, depth)
            if stop is not None:
                i = stop
                if b == 11:  # return from subr
                    return

    def _operand(self, b: int, cs: bytes, i: int) -> int:
        if b == 28:
            self.stack.append(struct.unpack(">h", cs[i : i + 2])[0])
            return i + 2
        if b <= 246:
            self.stack.append(b - 139)
        elif b <= 250:
            self.stack.append((b - 247) * 256 + cs[i] + 108)
            i += 1
        elif b <= 254:
            self.stack.append(-(b - 251) * 256 - cs[i] - 108)
            i += 1
        else:  # 255: 16.16 fixed point
            self.stack.append(struct.unpack(">i", cs[i : i + 4])[0] / 65536.0)
            i += 4
        return i

    def _operator(self, b: int, cs: bytes, i: int, depth: int):
        s = self.stack
        if b in (1, 3, 18, 23):  # hstem vstem hstemhm vstemhm
            self._take_width(even=True)
            self.nstems += len(s) // 2
            s.clear()
        elif b in (19, 20):  # hintmask / cntrmask
            self._take_width(even=True)
            self.nstems += len(s) // 2
            s.clear()
            return i + (self.nstems + 7) // 8  # skip the mask bytes
        elif b == 21:  # rmoveto
            self._take_width(even=True)
            self._moveto(s[0] if s else 0, s[1] if len(s) > 1 else 0)
            s.clear()
        elif b == 22:  # hmoveto
            self._take_width(even=False)
            self._moveto(s[0] if s else 0, 0)
            s.clear()
        elif b == 4:  # vmoveto
            self._take_width(even=False)
            self._moveto(0, s[0] if s else 0)
            s.clear()
        elif b == 5:  # rlineto
            for k in range(0, len(s) - 1, 2):
                self._lineto(s[k], s[k + 1])
            s.clear()
        elif b == 6:  # hlineto (alternating, starting horizontal)
            self._alt_lineto(s, horizontal=True)
            s.clear()
        elif b == 7:  # vlineto (alternating, starting vertical)
            self._alt_lineto(s, horizontal=False)
            s.clear()
        elif b == 8:  # rrcurveto
            for k in range(0, len(s) - 5, 6):
                self._curveto(*s[k : k + 6])
            s.clear()
        elif b == 24:  # rcurveline
            k = 0
            while k + 6 <= len(s) - 2:
                self._curveto(*s[k : k + 6])
                k += 6
            if k + 2 <= len(s):
                self._lineto(s[k], s[k + 1])
            s.clear()
        elif b == 25:  # rlinecurve
            k = 0
            while k + 2 <= len(s) - 6:
                self._lineto(s[k], s[k + 1])
                k += 2
            if k + 6 <= len(s):
                self._curveto(*s[k : k + 6])
            s.clear()
        elif b == 26:  # vvcurveto
            self._vvcurveto(s)
            s.clear()
        elif b == 27:  # hhcurveto
            self._hhcurveto(s)
            s.clear()
        elif b == 30:  # vhcurveto
            self._vhcurveto(s, start_vertical=True)
            s.clear()
        elif b == 31:  # hvcurveto
            self._vhcurveto(s, start_vertical=False)
            s.clear()
        elif b == 10:  # callsubr
            idx = int(s.pop()) + self.lbias if s else -1
            if 0 <= idx < len(self.subrs):
                self.run(self.subrs[idx], depth + 1)
            return i
        elif b == 29:  # callgsubr
            idx = int(s.pop()) + self.gbias if s else -1
            if 0 <= idx < len(self.gsubrs):
                self.run(self.gsubrs[idx], depth + 1)
            return i
        elif b == 11:  # return
            return i
        elif b == 14:  # endchar
            self._take_width(even=True)
            self.finish()
            self._done = True
        elif b == 12:  # escape: two-byte operator
            b2 = cs[i]
            i += 1
            self._escape(b2)
            return i
        else:  # reserved / unsupported
            s.clear()
        return i

    # -- alternating line/curve operators ------------------------------
    def _alt_lineto(self, s: List[float], horizontal: bool) -> None:
        for v in s:
            if horizontal:
                self._lineto(v, 0)
            else:
                self._lineto(0, v)
            horizontal = not horizontal

    def _vvcurveto(self, s: List[float]) -> None:
        i = 0
        dx1 = 0.0
        if len(s) % 4 == 1:
            dx1 = s[0]
            i = 1
        while i + 4 <= len(s):
            self._curveto(dx1, s[i], s[i + 1], s[i + 2], 0, s[i + 3])
            dx1 = 0.0
            i += 4

    def _hhcurveto(self, s: List[float]) -> None:
        i = 0
        dy1 = 0.0
        if len(s) % 4 == 1:
            dy1 = s[0]
            i = 1
        while i + 4 <= len(s):
            self._curveto(s[i], dy1, s[i + 1], s[i + 2], s[i + 3], 0)
            dy1 = 0.0
            i += 4

    def _vhcurveto(self, s: List[float], start_vertical: bool) -> None:
        """vhcurveto / hvcurveto: curves alternating start tangent axis.

        Each curve is 4 operands; a final 5th operand (df) sets the
        otherwise-zero free coordinate of the last curve's endpoint.
        """
        i = 0
        n = len(s)
        vertical = start_vertical
        while i + 4 <= n:
            last = i + 8 > n
            df = s[i + 4] if last and (n - i) == 5 else 0.0
            if vertical:
                self._curveto(0, s[i], s[i + 1], s[i + 2], s[i + 3], df)
            else:
                self._curveto(s[i], 0, s[i + 1], s[i + 2], df, s[i + 3])
            vertical = not vertical
            i += 4

    def _escape(self, b2: int) -> None:
        s = self.stack
        if b2 == 35:  # flex
            if len(s) >= 12:
                self._curveto(*s[0:6])
                self._curveto(*s[6:12])
            s.clear()
        elif b2 == 34:  # hflex: dx1 dx2 dy2 dx3 dx4 dx5 dx6
            if len(s) >= 7:
                self._curveto(s[0], 0, s[1], s[2], s[3], 0)
                self._curveto(s[4], 0, s[5], -s[2], s[6], 0)
            s.clear()
        elif b2 == 36:  # hflex1: dx1 dy1 dx2 dy2 dx3 dx4 dx5 dy5 dx6
            if len(s) >= 9:
                self._curveto(s[0], s[1], s[2], s[3], s[4], 0)
                dy = -(s[1] + s[3] + s[7])
                self._curveto(s[5], 0, s[6], s[7], s[8], dy)
            s.clear()
        elif b2 == 37:  # flex1
            if len(s) >= 11:
                dx = s[0] + s[2] + s[4] + s[6] + s[8]
                dy = s[1] + s[3] + s[5] + s[7] + s[9]
                self._curveto(s[0], s[1], s[2], s[3], s[4], s[5])
                if abs(dx) > abs(dy):
                    self._curveto(s[6], s[7], s[8], s[9], s[10], -dy)
                else:
                    self._curveto(s[6], s[7], s[8], s[9], -dx, s[10])
            s.clear()
        else:  # arithmetic / unsupported: discard operands
            s.clear()
