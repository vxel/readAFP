"""CCITT T.6 (Group 4 / IBM MMR) bilevel image decompression.

IOCA compression X'01' (IBM MMR) is ITU-T T.6 Group-4 two-dimensional
coding: every line is coded against the line above it (an imaginary all
white line above the first), using pass / horizontal / vertical modes and
the modified-Huffman white and black run-length code tables from T.4.

`decode_g4` returns the pel raster as rows of ``(width + 7) // 8`` bytes,
MSB first, with 1 = black — the same convention IOCA uses for an
uncompressed bilevel image, so the caller packs it to PNG unchanged.

Reference: ITU-T Recommendation T.6; IOCA Reference AFPC-0003-09,
Appendix A (docs/specs/ioca-reference-09.pdf).
"""

from typing import Dict, List, Optional, Tuple

# White run-length codes: run -> bit string (T.4 Table 1/2).
_WHITE = {
    0: "00110101", 1: "000111", 2: "0111", 3: "1000", 4: "1011", 5: "1100",
    6: "1110", 7: "1111", 8: "10011", 9: "10100", 10: "00111", 11: "01000",
    12: "001000", 13: "000011", 14: "110100", 15: "110101", 16: "101010",
    17: "101011", 18: "0100111", 19: "0001100", 20: "0001000", 21: "0010111",
    22: "0000011", 23: "0000100", 24: "0101000", 25: "0101011", 26: "0010011",
    27: "0100100", 28: "0011000", 29: "00000010", 30: "00000011",
    31: "00011010", 32: "00011011", 33: "00010010", 34: "00010011",
    35: "00010100", 36: "00010101", 37: "00010110", 38: "00010111",
    39: "00101000", 40: "00101001", 41: "00101010", 42: "00101011",
    43: "00101100", 44: "00101101", 45: "00000100", 46: "00000101",
    47: "00001010", 48: "00001011", 49: "01010010", 50: "01010011",
    51: "01010100", 52: "01010101", 53: "00100100", 54: "00100101",
    55: "01011000", 56: "01011001", 57: "01011010", 58: "01011011",
    59: "01001010", 60: "01001011", 61: "00110010", 62: "00110011",
    63: "00110100",
    64: "11011", 128: "10010", 192: "010111", 256: "0110111", 320: "00110110",
    384: "00110111", 448: "01100100", 512: "01100101", 576: "01101000",
    640: "01100111", 704: "011001100", 768: "011001101", 832: "011010010",
    896: "011010011", 960: "011010100", 1024: "011010101", 1088: "011010110",
    1152: "011010111", 1216: "011011000", 1280: "011011001", 1344: "011011010",
    1408: "011011011", 1472: "010011000", 1536: "010011001", 1600: "010011010",
    1664: "011000", 1728: "010011011",
}

# Black run-length codes: run -> bit string (T.4 Table 1/2).
_BLACK = {
    0: "0000110111", 1: "010", 2: "11", 3: "10", 4: "011", 5: "0011",
    6: "0010", 7: "00011", 8: "000101", 9: "000100", 10: "0000100",
    11: "0000101", 12: "0000111", 13: "00000100", 14: "00000111",
    15: "000011000", 16: "0000010111", 17: "0000011000", 18: "0000001000",
    19: "00001100111", 20: "00001101000", 21: "00001101100", 22: "00000110111",
    23: "00000101000", 24: "00000010111", 25: "00000011000", 26: "000011001010",
    27: "000011001011", 28: "000011001100", 29: "000011001101",
    30: "000001101000", 31: "000001101001", 32: "000001101010",
    33: "000001101011", 34: "000011010010", 35: "000011010011",
    36: "000011010100", 37: "000011010101", 38: "000011010110",
    39: "000011010111", 40: "000001101100", 41: "000001101101",
    42: "000011011010", 43: "000011011011", 44: "000001010100",
    45: "000001010101", 46: "000001010110", 47: "000001010111",
    48: "000001100100", 49: "000001100101", 50: "000001010010",
    51: "000001010011", 52: "000000100100", 53: "000000110111",
    54: "000000111000", 55: "000000100111", 56: "000000101000",
    57: "000001011000", 58: "000001011001", 59: "000000101011",
    60: "000000101100", 61: "000001011010", 62: "000001100110",
    63: "000001100111",
    64: "0000001111", 128: "000011001000", 192: "000011001001",
    256: "000001011011", 320: "000000110011", 384: "000000110100",
    448: "000000110101", 512: "0000001101100", 576: "0000001101101",
    640: "0000001001010", 704: "0000001001011", 768: "0000001001100",
    832: "0000001001101", 896: "0000001110010", 960: "0000001110011",
    1024: "0000001110100", 1088: "0000001110101", 1152: "0000001110110",
    1216: "0000001110111", 1280: "0000001010010", 1344: "0000001010011",
    1408: "0000001010100", 1472: "0000001010101", 1536: "0000001011010",
    1600: "0000001011011", 1664: "0000001100100", 1728: "0000001100101",
}

# Extended make-up codes, common to both colors (T.4 Table 3).
_EXT = {
    1792: "00000001000", 1856: "00000001100", 1920: "00000001101",
    1984: "000000010010", 2048: "000000010011", 2112: "000000010100",
    2176: "000000010101", 2240: "000000010110", 2304: "000000010111",
    2368: "000000011100", 2432: "000000011101", 2496: "000000011110",
    2560: "000000011111",
}

# 2D mode codes (T.6): bit string -> ("P" | "H" | "V", delta).
_MODES = {
    "0001": ("P", 0), "001": ("H", 0), "1": ("V", 0),
    "011": ("V", 1), "010": ("V", -1),
    "000011": ("V", 2), "000010": ("V", -2),
    "0000011": ("V", 3), "0000010": ("V", -3),
}


def _build(codes: Dict[int, str]) -> Tuple[Dict[Tuple[int, int], int], int]:
    """Turn {run: bitstring} into {(length, value): run} plus max length."""
    table: Dict[Tuple[int, int], int] = {}
    longest = 0
    for run, bits in codes.items():
        table[(len(bits), int(bits, 2))] = run
        longest = max(longest, len(bits))
    return table, longest


_WHITE_T, _WHITE_MAX = _build({**_WHITE, **_EXT})
_BLACK_T, _BLACK_MAX = _build({**_BLACK, **_EXT})
_MODE_LOOKUP = {(len(b), int(b, 2)): m for b, m in _MODES.items()}
_MODE_MAX = max(len(b) for b in _MODES)


class _Bits:
    """MSB-first bit reader over a byte string."""

    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.end = len(data) * 8

    def bit(self) -> Optional[int]:
        if self.pos >= self.end:
            return None
        b = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b


def _read_mode(bits: _Bits) -> Optional[Tuple[str, int]]:
    val = 0
    for length in range(1, _MODE_MAX + 1):
        b = bits.bit()
        if b is None:
            return None
        val = (val << 1) | b
        mode = _MODE_LOOKUP.get((length, val))
        if mode is not None:
            return mode
    return None


# Longest leading-zero prefix of any valid line-start code: the T.6 2D
# extension escape 0000001 (6 zeros). A zero run at least this long + 1 marks
# an inter-line EOL / fill, never a mode. Producers use the full 11-zero G3
# EOL or a shorter byte-fill variant, so key off the "too long to be a code"
# threshold rather than an exact length.
_EOL_MIN_ZEROS = 7


def _maybe_skip_eol(bits: _Bits) -> bool:
    """Consume an inter-line EOL / fill (>= _EOL_MIN_ZEROS zeros then a 1).

    T.6 lines are coded back-to-back, but IBM MMR separates the 1D first
    line from the 2D remainder with an EOL — the classic 11-zero G3 EOL or a
    shorter byte-fill marker. A real 2D mode/extension code begins with at
    most 6 zeros, so a longer zero run at a line boundary unambiguously marks
    an EOL; shorter is left alone. Returns True if an EOL was consumed.
    """
    save = bits.pos
    zeros = 0
    while True:
        b = bits.bit()
        if b is None:
            bits.pos = save
            return False
        if b == 0:
            zeros += 1
            continue
        # hit a 1
        if zeros >= _EOL_MIN_ZEROS:
            return True  # consumed the whole EOL (zeros + terminating 1)
        bits.pos = save
        return False


def _decode_1d_line(bits: _Bits, width: int) -> List[int]:
    """Decode one 1D (Modified Huffman) scan line to changing elements.

    IBM MMR (IOCA compression X'01') codes the *first* scan line 1D:
    alternating white/black runs, starting white, each a sum of make-up
    codes plus a terminating code, laid down until the line is filled.
    """
    cur: List[int] = []
    a0 = 0
    color = 0  # 0 = white, 1 = black
    while a0 < width:
        table = _BLACK_T if color else _WHITE_T
        longest = _BLACK_MAX if color else _WHITE_MAX
        run = _read_run(bits, table, longest)
        if run is None:
            break
        a0 = min(a0 + run, width)
        cur.append(a0)
        color ^= 1
    return cur


def _read_run(bits: _Bits, table: Dict[Tuple[int, int], int],
              longest: int) -> Optional[int]:
    """Decode one full run length (make-up codes summed + a terminating code)."""
    total = 0
    while True:
        val = 0
        run = None
        for length in range(1, longest + 1):
            b = bits.bit()
            if b is None:
                return None
            val = (val << 1) | b
            run = table.get((length, val))
            if run is not None:
                break
        if run is None:
            return None
        total += run
        if run < 64:  # terminating code ends the run
            return total


def _decode_2d_line(bits: _Bits, ref: List[int], width: int) -> List[int]:
    """Decode one T.6 (2D) scan line against the reference line."""
    cur: List[int] = []
    a0 = -1
    color = 0  # 0 = white, 1 = black
    while a0 < width:
        # b1 = first changing element on the reference line right of a0 of
        # the opposite color to a0; b2 = the one after it.
        i = 0
        while i < len(ref) and ref[i] <= a0:
            i += 1
        if i < len(ref) and (i & 1) != color:
            i += 1
        b1 = ref[i] if i < len(ref) else width
        b2 = ref[i + 1] if i + 1 < len(ref) else width

        mode = _read_mode(bits)
        if mode is None:
            break  # stream exhausted / corrupt: stop this row
        kind, delta = mode
        start = a0 if a0 >= 0 else 0
        if kind == "P":  # pass: skip to b2, color unchanged
            a0 = b2
        elif kind == "V":  # vertical: change at b1 + delta
            a1 = max(0, min(b1 + delta, width))
            cur.append(a1)
            a0 = a1
            color ^= 1
        else:  # horizontal: two runs, current color then the other
            table1 = _BLACK_T if color else _WHITE_T
            max1 = _BLACK_MAX if color else _WHITE_MAX
            table2 = _WHITE_T if color else _BLACK_T
            max2 = _WHITE_MAX if color else _BLACK_MAX
            run1 = _read_run(bits, table1, max1)
            run2 = _read_run(bits, table2, max2)
            if run1 is None or run2 is None:
                break
            a1 = min(start + run1, width)
            a2 = min(a1 + run2, width)
            cur.append(a1)
            cur.append(a2)
            a0 = a2
    return cur


def decode_g4(data: bytes, width: int, height: int) -> bytes:
    """Decode an IBM MMR (IOCA X'01') stream to a 1-bpp raster, 1 = black.

    Returns ``(width + 7) // 8 * height`` bytes (rows padded to a byte),
    MSB first. Decoding stops early and pads with white on malformed data
    rather than raising, so a truncated stream still yields a partial image.

    IBM MMR is IBM's variant of ITU-T T.6: the *first* scan line is coded
    1D (Modified Huffman) and the rest 2D, with a single EOL marking the
    1D->2D switch (plus a leading EOL these producers prepend, and an EOP at
    the end). Each EOL carries a 1-bit 1D/2D tag for the line that follows.
    Decoding every line as 2D — as pure T.6 would — mis-reads the tag bit
    and 1D first line, desyncing until the switch EOL happens to re-align it
    (the "black bands across the top" failure on full-page scans).
    """
    if width <= 0 or height <= 0:
        return b""
    bits = _Bits(data)
    row_bytes = (width + 7) // 8
    out = bytearray()
    # Reference line as a list of changing-element positions, terminated by
    # two width sentinels. The imaginary line above row 0 is all white.
    ref: List[int] = [width, width]

    for _ in range(height):
        # An EOL frames the data (leading EOL, the 1D->2D switch, the EOP).
        # Each is followed by a 1-bit 1D/2D tag: the leading EOL's tag is 1
        # (first line 1D), the switch EOL's is 0 (2D thereafter). A correctly
        # aligned 2D line never starts with that many zeros, so this only
        # fires on real EOLs.
        one_d = False
        if _maybe_skip_eol(bits):
            one_d = bits.bit() == 1
        if bits.pos >= bits.end:
            break
        cur = (_decode_1d_line(bits, width) if one_d
               else _decode_2d_line(bits, ref, width))
        # Rasterize this row from its changing elements: runs alternate
        # starting white, flipping color at each transition.
        rowbuf = bytearray(row_bytes)
        pos = 0
        black = False
        for t in cur:
            t = max(0, min(t, width))
            if black:  # paint black run [pos, t)
                for x in range(pos, t):
                    rowbuf[x >> 3] |= 0x80 >> (x & 7)
            pos = t
            black = not black
        if black:  # trailing black run to the row's end
            for x in range(pos, width):
                rowbuf[x >> 3] |= 0x80 >> (x & 7)
        out += rowbuf
        ref = cur + [width, width]

    # Pad rows we couldn't decode (truncated/exhausted stream) with white so
    # the raster is always the full height the caller expects.
    if len(out) < row_bytes * height:
        out += bytes(row_bytes * height - len(out))
    return bytes(out)
