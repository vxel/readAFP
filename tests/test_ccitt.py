"""Round-trip tests for the CCITT G4 and TIFF LZW image decoders.

The decoders are validated visually against real AFP images, but those are
confidential; here small test-only encoders round-trip arbitrary rasters
through the production decoders so regressions are caught in CI.
"""

from readafp.ccitt import (
    _BLACK,
    _EXT,
    _WHITE,
    _MODES,
    decode_g4,
)
from readafp.ioca import _decode_lzw


# --- test-only encoders -----------------------------------------------------

def _changing_elements(row):
    """Positions where the row's color flips, starting from white."""
    ce = []
    prev = 0
    for x, px in enumerate(row):
        if px != prev:
            ce.append(x)
            prev = px
    return ce


def _run_bits(run, white):
    codes = dict(_WHITE if white else _BLACK)
    codes.update(_EXT)
    bits = ""
    while run >= 64:
        # largest make-up code <= run
        makeup = max(m for m in codes if m >= 64 and m <= run)
        bits += codes[makeup]
        run -= makeup
    bits += codes[run]  # terminating code (0-63)
    return bits


_MODE_BITS = {v: k for k, v in _MODES.items()}


def _encode_1d(row, width):
    """Encode one row as 1D Modified Huffman runs (starting white)."""
    bits = ""
    pos = 0
    color = 0
    for x in _changing_elements(row):
        bits += _run_bits(x - pos, white=(color == 0))
        pos = x
        color ^= 1
    bits += _run_bits(width - pos, white=(color == 0))  # final run to width
    return bits


def _encode_g4(rows, width):
    """Encode a bilevel raster (list of rows of 0/1) as an IBM MMR stream.

    Mirrors real AFP MMR (IOCA X'01') framing: a leading EOL + 1D tag, the
    first line 1D, a switch EOL + 2D tag, then the remaining lines 2D. This
    exercises the decoder's 1D and 2D paths and its EOL+tag handling.
    """
    bits = "000000000001" + "1"  # leading EOL + 1D tag
    bits += _encode_1d(rows[0], width)
    bits += "000000000001" + "0"  # 1D->2D switch EOL + 2D tag
    ref = _changing_elements(rows[0]) + [width, width]
    for row in rows[1:]:
        cur = _changing_elements(row) + [width, width]
        a0 = -1
        color = 0
        ci = 0  # index into cur
        while a0 < width:
            # b1/b2 on the reference line (same rule as the decoder)
            i = 0
            while i < len(ref) and ref[i] <= a0:
                i += 1
            if i < len(ref) and (i & 1) != color:
                i += 1
            b1 = ref[i] if i < len(ref) else width
            b2 = ref[i + 1] if i + 1 < len(ref) else width
            # a1 = next real changing element on this line > a0
            while ci < len(cur) and cur[ci] <= a0:
                ci += 1
            a1 = cur[ci] if ci < len(cur) else width
            if b2 < a1:  # pass mode
                bits += _MODE_BITS[("P", 0)]
                a0 = b2
            elif abs(a1 - b1) <= 3:  # vertical mode
                bits += _MODE_BITS[("V", a1 - b1)]
                a0 = a1
                color ^= 1
            else:  # horizontal mode: two runs from a0 (>=0)
                a2 = cur[ci + 1] if ci + 1 < len(cur) else width
                start = a0 if a0 >= 0 else 0
                bits += _MODE_BITS[("H", 0)]
                bits += _run_bits(a1 - start, white=(color == 0))
                bits += _run_bits(a2 - a1, white=(color == 1))
                a0 = a2
        ref = _changing_elements(row) + [width, width]
    # pad to a byte
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def _encode_lzw(data):
    """Minimal TIFF-variant LZW encoder (MSB-first, early change)."""
    CLEAR, EOI = 256, 257
    table = {bytes([i]): i for i in range(256)}
    nxt, width = 258, 9
    out_bits = ""

    def emit(code, w):
        return format(code, "0%db" % w)

    out_bits += emit(CLEAR, width)
    w = b""
    for b in data:
        wc = w + bytes([b])
        if wc in table:
            w = wc
        else:
            out_bits += emit(table[w], width)
            table[wc] = nxt
            nxt += 1
            # The decoder adds entries one code behind the encoder, so it
            # bumps width at nxt == 2**w - 1 (early change); the encoder must
            # bump one entry later to stay in step.
            if nxt == (1 << width) and width < 12:
                width += 1
            w = bytes([b])
    if w:
        out_bits += emit(table[w], width)
    out_bits += emit(EOI, width)
    out_bits += "0" * (-len(out_bits) % 8)
    return int(out_bits, 2).to_bytes(len(out_bits) // 8, "big")


# --- tests ------------------------------------------------------------------

def _raster_to_rows(raw, width, height):
    rows = []
    rb = (width + 7) // 8
    for y in range(height):
        row = [(raw[y * rb + (x >> 3)] >> (7 - (x & 7))) & 1 for x in range(width)]
        rows.append(row)
    return rows


def test_g4_round_trips_a_patterned_image():
    width, height = 24, 12
    # A frame plus a diagonal — exercises horizontal, vertical and pass modes.
    rows = []
    for y in range(height):
        row = [0] * width
        if y in (0, height - 1):
            row = [1] * width
        else:
            row[0] = row[width - 1] = 1
            row[y] = 1
            row[width - 1 - y] = 1
        rows.append(row)
    stream = _encode_g4(rows, width)
    raw = decode_g4(stream, width, height)
    assert _raster_to_rows(raw, width, height) == rows


def test_g4_first_line_has_content():
    # IBM MMR codes the first scan line 1D. A first row with real content
    # (not trivially white) desynced the old all-2D decoder, painting black
    # bands across the top of full-page scans until it re-aligned lower down.
    width, height = 40, 20
    rows = []
    for y in range(height):
        row = [0] * width
        # dense marks on the very first row, sparser below
        for x in range(0, width, 2 if y == 0 else 7):
            row[x] = 1
        rows.append(row)
    raw = decode_g4(_encode_g4(rows, width), width, height)
    assert _raster_to_rows(raw, width, height) == rows


def test_g4_all_white_and_all_black():
    width, height = 16, 4
    for fill in (0, 1):
        rows = [[fill] * width for _ in range(height)]
        raw = decode_g4(_encode_g4(rows, width), width, height)
        assert _raster_to_rows(raw, width, height) == rows


def test_g4_truncated_stream_pads_white():
    # A stream that ends early still yields the full raster (white padding).
    raw = decode_g4(b"\x00\x01", 32, 10)
    assert len(raw) == ((32 + 7) // 8) * 10


def test_lzw_round_trips():
    for data in (b"", b"A", b"AAAAAAAA", b"ABABABABAB" * 20,
                 bytes(range(256)) * 4, b"\x00" * 1000):
        assert _decode_lzw(_encode_lzw(data)) == data
