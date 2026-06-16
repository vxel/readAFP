"""Tests for the CFF / Type 2 charstring interpreter (``readafp.cff``).

Two layers of validation:

* Stdlib-only regression tests run against the checked-in fixtures
  (``testdata/cff_sample.otf``, ``cff_cid_sample.cff``,
  ``foca_cff_sample.afp``) with hard-coded expectations, so the parser is
  covered even where fontTools is absent.

* Oracle tests (skipped when fontTools is not installed) compare every
  glyph's decoded outline and advance width against fontTools reading the
  same bytes — an independent Type 2 implementation. These also build
  small fonts on the fly to exercise operators the realistic fixture's
  charstring specializer does not emit (rrcurveto, flex, local/global
  subroutines).
"""

from io import BytesIO
from pathlib import Path

import pytest

from readafp import cff
from readafp.foca import parse_fonts
from readafp.parser import iter_fields

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
PLAIN_OTF = TESTDATA / "cff_sample.otf"
CID_CFF = TESTDATA / "cff_cid_sample.cff"
FOCA_CFF = TESTDATA / "foca_cff_sample.afp"
CFF_DOC = TESTDATA / "cff_document_sample.afp"


def _plain_cff_bytes() -> bytes:
    """Raw CFF table bytes sliced from the plain OpenType fixture."""
    data = PLAIN_OTF.read_bytes()
    # Minimal sfnt table-directory walk to find the 'CFF ' table.
    num_tables = int.from_bytes(data[4:6], "big")
    pos = 12
    for _ in range(num_tables):
        tag = data[pos : pos + 4]
        offset = int.from_bytes(data[pos + 8 : pos + 12], "big")
        length = int.from_bytes(data[pos + 12 : pos + 16], "big")
        if tag == b"CFF ":
            return data[offset : offset + length]
        pos += 16
    raise AssertionError("no CFF table in fixture")


# ---------------------------------------------------------------------------
# Stdlib-only regression tests (no fontTools required)
# ---------------------------------------------------------------------------
def test_plain_font_glyph_names_and_metrics() -> None:
    font = cff.CFFFont(_plain_cff_bytes())
    assert not font.is_cid
    assert font.units_per_em == 1000
    assert font.glyph_names == [".notdef", "A", "H", "O", "period"]
    assert font.has_glyph("period")
    assert not font.has_glyph("Q")


def test_plain_font_curve_outline() -> None:
    """The 'period' dot is two cubic curves with known control points."""
    glyph = cff.CFFFont(_plain_cff_bytes()).glyph("period")
    assert glyph.advance == 250.0
    assert glyph.segments == [
        ("m", 100.0, 0.0),
        ("c", 160.0, 0.0, 160.0, 120.0, 100.0, 120.0),
        ("c", 40.0, 120.0, 40.0, 0.0, 100.0, 0.0),
        ("z",),
    ]


def test_plain_font_advances_differ_per_glyph() -> None:
    font = cff.CFFFont(_plain_cff_bytes())
    assert font.glyph("A").advance == 500.0
    assert font.glyph("H").advance == 600.0
    assert font.glyph("O").advance == 550.0


def test_cid_font_detection_and_naming() -> None:
    font = cff.CFFFont(CID_CFF.read_bytes())
    assert font.is_cid
    assert font.glyph_names == ["cid00000", "cid00001", "cid00002"]


def test_cid_fdselect_routes_to_distinct_widths() -> None:
    """Glyph 1 -> FD0, glyph 2 -> FD1; the advances prove the routing."""
    font = cff.CFFFont(CID_CFF.read_bytes())
    g1 = font.glyph_by_gid(1)
    g2 = font.glyph_by_gid(2)
    assert g1.advance == 400.0
    assert g1.segments == [("m", 100.0, 0.0), ("l", 300.0, 0.0), ("z",)]
    assert g2.advance == 250.0
    assert g2.segments == [("m", 50.0, 0.0), ("l", 50.0, 300.0), ("z",)]


def test_glyph_by_gid_out_of_range() -> None:
    font = cff.CFFFont(_plain_cff_bytes())
    assert font.glyph_by_gid(999) is None
    assert font.glyph("nonexistent") is None


# ---- pure parsing helpers -------------------------------------------------
def test_subr_bias() -> None:
    assert cff._subr_bias(0) == 107
    assert cff._subr_bias(1239) == 107
    assert cff._subr_bias(1240) == 1131
    assert cff._subr_bias(33899) == 1131
    assert cff._subr_bias(33900) == 32768


def test_parse_fdselect_format0() -> None:
    data = bytes([0]) + bytes([0, 1, 1, 0])  # format 0, four glyphs
    assert cff._parse_fdselect(data, 0, 4) == [0, 1, 1, 0]


def test_parse_fdselect_format3() -> None:
    import struct

    data = (
        bytes([3])
        + struct.pack(">H", 2)        # 2 ranges
        + struct.pack(">H", 0) + bytes([0])   # gid 0.. -> FD0
        + struct.pack(">H", 3) + bytes([1])   # gid 3.. -> FD1
        + struct.pack(">H", 5)        # sentinel
    )
    assert cff._parse_fdselect(data, 0, 5) == [0, 0, 0, 1, 1]


def test_parse_charset_format0() -> None:
    import struct

    data = bytes([0]) + struct.pack(">HH", 5, 9)  # SIDs for glyphs 1,2
    assert cff._parse_charset(data, 0, 3) == [0, 5, 9]


def test_parse_charset_format1_ranges() -> None:
    import struct

    # one range starting at SID 10, nLeft=2 -> 10,11,12 for glyphs 1..3
    data = bytes([1]) + struct.pack(">H", 10) + bytes([2])
    assert cff._parse_charset(data, 0, 4) == [0, 10, 11, 12]


def test_real_number_operand() -> None:
    # FontMatrix with a real (operand 30) 0.001 should give upm 1000.
    font = cff.CFFFont(_plain_cff_bytes())
    assert round(font.font_matrix[0], 6) == 0.001


# ---------------------------------------------------------------------------
# foca integration (no fontTools required)
# ---------------------------------------------------------------------------
def test_foca_cff_pipeline_decodes_outlines() -> None:
    fonts = parse_fonts(list(iter_fields(FOCA_CFF.read_bytes())))
    assert len(fonts) == 1
    font = fonts[0]
    assert font.is_outline
    assert font.outline_format == "CFF / CID-keyed outline"
    assert font.units_per_em == 1000
    # Every GCGID resolved through FNN to a real CFF outline glyph.
    assert set(font.outline_glyphs) == {
        "LA010000", "LH010000", "LO010000", "PD010000"
    }
    advances = {g: font.outline_glyphs[g].advance for g in font.outline_glyphs}
    assert advances == {
        "LA010000": 500.0, "LH010000": 600.0,
        "LO010000": 550.0, "PD010000": 250.0,
    }
    # The 'O' glyph carries two contours (outer + inner) -> two move/close.
    o_segs = font.outline_glyphs["LO010000"].segments
    assert sum(1 for s in o_segs if s[0] == "m") == 2


def test_foca_cff_renders_real_paths() -> None:
    from readafp import ptoca, render

    pages = ptoca.extract_pages(list(iter_fields(FOCA_CFF.read_bytes())))
    assert pages
    svg = render.page_to_svg(pages[0])
    # One <path> per decoded glyph outline (not a metadata fallback sheet).
    assert svg.count("<path") == 4


# ---------------------------------------------------------------------------
# Document text drawn in an embedded CFF outline font (Phase B)
# ---------------------------------------------------------------------------
def test_cff_document_draws_text_as_outline_paths() -> None:
    """The page's TRN run renders as embedded CFF outlines, not substitutes."""
    from readafp import ptoca

    pages = ptoca.extract_pages(list(iter_fields(CFF_DOC.read_bytes())))
    assert len(pages) == 1
    page = pages[0]
    # No substitute-font text runs: every byte resolved to a real glyph.
    assert page.texts == []
    # One vector graphic carries the whole run as a single path.
    assert len(page.graphics) == 1
    vg = page.graphics[0]
    # gps_w is the exact sum of the four glyph advances (500+600+550+250).
    assert vg.graphic.gps_w == 1900
    # A + H + O(two contours) + period = 5 subpaths.
    assert vg.graphic.svg.count("M") == 5
    # Positioned at the PTX cursor (inline 1000; baseline 2000 - ascent).
    assert vg.x == 1000
    assert vg.y < 2000


def test_cff_document_advance_matches_font() -> None:
    """The run's design-space width equals the embedded font's advances."""
    from readafp import ptoca

    page = ptoca.extract_pages(list(iter_fields(CFF_DOC.read_bytes())))[0]
    font = cff.CFFFont(_plain_cff_bytes())
    expected = sum(font.glyph(n).advance for n in ("A", "H", "O", "period"))
    assert page.graphics[0].graphic.gps_w == expected


def test_cff_document_renders() -> None:
    from readafp import ptoca, render

    page = ptoca.extract_pages(list(iter_fields(CFF_DOC.read_bytes())))[0]
    svg = render.page_to_svg(page)
    assert svg.count("<path") == 1
    assert "<text" not in svg  # nothing fell back to substitute text


# ---------------------------------------------------------------------------
# Oracle tests against fontTools (skipped when fontTools is unavailable)
# ---------------------------------------------------------------------------
def _round_segs(segs):
    return [
        tuple(round(v, 2) if isinstance(v, float) else v for v in s)
        for s in segs
    ]


def _oracle_segs(glyph_set, name):
    """fontTools pen output as readafp-style segment tuples."""
    from fontTools.pens.recordingPen import RecordingPen

    pen = RecordingPen()
    glyph_set[name].draw(pen)
    out = []
    for op, args in pen.value:
        if op == "moveTo":
            out.append(("m",) + args[0])
        elif op == "lineTo":
            out.append(("l",) + args[0])
        elif op == "curveTo":
            out.append(("c",) + args[0] + args[1] + args[2])
        elif op == "closePath":
            out.append(("z",))
    return _round_segs(out)


@pytest.fixture
def ttlib():
    return pytest.importorskip("fontTools.ttLib")


def test_oracle_plain_font_all_glyphs(ttlib) -> None:
    tt = ttlib.TTFont(str(PLAIN_OTF))
    font = cff.CFFFont(tt.reader["CFF "])
    glyph_set = tt.getGlyphSet()
    hmtx = tt["hmtx"]
    for name in tt.getGlyphOrder():
        mine = font.glyph(name)
        assert _round_segs(mine.segments) == _oracle_segs(glyph_set, name)
        assert mine.advance == hmtx[name][0]


def _build_otf(programs, advances):
    """Compile glyphs given as explicit Type 2 programs; return (cff, ttfont).

    ``programs`` maps glyph name -> Type 2 program list. fontTools encodes
    the program verbatim (no re-specialization), so each operator we author
    survives into the bytes both interpreters then read.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.misc.psCharStrings import T2CharString
    from fontTools.ttLib import TTFont

    order = [".notdef"] + [n for n in programs if n != ".notdef"]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({})
    charstrings = {n: T2CharString(program=p) for n, p in programs.items()}
    charstrings.setdefault(
        ".notdef", T2CharString(program=[0, 0, "rmoveto", "endchar"]))
    fb.setupCFF("X", {}, charstrings, {})
    fb.setupHorizontalMetrics({n: (advances.get(n, 0), 0) for n in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "X", "styleName": "R"})
    fb.setupOS2()
    fb.setupPost()
    buf = BytesIO()
    fb.save(buf)
    buf.seek(0)
    tt = TTFont(buf)
    return tt.reader["CFF "], tt


def test_oracle_operator_coverage(ttlib) -> None:
    """Exercise operators the realistic fixture's specializer omits."""
    programs = {
        # rrcurveto + hlineto + rlinecurve + flex (escape 12 35)
        "curvy": [
            100, 0, "rmoveto",
            50, 60, 40, 0, 50, -60, "rrcurveto",
            30, "hlineto",
            0, 40, 30, 30, 30, 0, 30, -30, "rlinecurve",
            20, 30, 20, -30, 20, 0, 20, 0, 20, 30, 20, -30, 50, "flex",
            "endchar",
        ],
        # vvcurveto + rcurveline (6n+2 operands) + vmoveto
        "vv": [
            0, 200, "vmoveto",
            30, 60, 40, 50, "vvcurveto",
            40, 50, 60, 40, 50, 60, 30, 20, "rcurveline",
            "endchar",
        ],
    }
    cff_bytes, tt = _build_otf(programs, {"curvy": 400, "vv": 350})
    font = cff.CFFFont(cff_bytes)
    glyph_set = tt.getGlyphSet()
    for name in programs:
        assert _round_segs(font.glyph(name).segments) == _oracle_segs(
            glyph_set, name)


def test_oracle_local_and_global_subrs(ttlib) -> None:
    """callgsubr must reproduce the inlined outline.

    The global subr has to exist before the font is compiled (the bbox
    pass walks the charstrings), so it is injected on the FontBuilder
    before saving.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.misc.psCharStrings import T2CharString
    from fontTools.ttLib import TTFont

    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder([".notdef", "sub"])
    fb.setupCharacterMap({})
    # Main calls global subr 0 (count 1 -> bias 107 -> operand -107).
    main = T2CharString(
        program=[100, 100, "rmoveto", -107, "callgsubr", "endchar"])
    notdef = T2CharString(program=[0, 0, "rmoveto", "endchar"])
    fb.setupCFF("X", {}, {".notdef": notdef, "sub": main}, {})
    fb.setupHorizontalMetrics({".notdef": (0, 0), "sub": (500, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "X", "styleName": "R"})
    fb.setupOS2()
    fb.setupPost()
    # Inject the global subr (drawing a triangle) before compilation.
    fb.font["CFF "].cff.GlobalSubrs.append(
        T2CharString(program=[200, 0, "rlineto", -100, 200, "rlineto",
                              "return"]))
    buf = BytesIO()
    fb.save(buf)
    buf.seek(0)
    tt = TTFont(buf)

    font = cff.CFFFont(tt.reader["CFF "])
    assert _round_segs(font.glyph("sub").segments) == _oracle_segs(
        tt.getGlyphSet(), "sub")
    # The inlined subr path is present (move + 2 lines + close).
    assert font.glyph("sub").segments[0] == ("m", 100.0, 100.0)
    assert len(font.glyph("sub").segments) == 4


def test_oracle_cid_font_all_glyphs(ttlib) -> None:
    from io import BytesIO as _B

    from fontTools.cffLib import CFFFontSet

    raw = CID_CFF.read_bytes()
    cffset = CFFFontSet()
    cffset.decompile(_B(raw), None)
    td = cffset[cffset.fontNames[0]]
    assert hasattr(td, "ROS")  # fontTools agrees it is CID-keyed
    glyph_set = {n: td.CharStrings[n] for n in td.charset}

    font = cff.CFFFont(raw)
    for gid, name in enumerate(td.charset):
        from fontTools.pens.recordingPen import RecordingPen

        pen = RecordingPen()
        td.CharStrings[name].draw(pen)
        oracle = []
        for op, args in pen.value:
            if op == "moveTo":
                oracle.append(("m",) + args[0])
            elif op == "lineTo":
                oracle.append(("l",) + args[0])
            elif op == "curveTo":
                oracle.append(("c",) + args[0] + args[1] + args[2])
            elif op == "closePath":
                oracle.append(("z",))
        assert _round_segs(font.glyph_by_gid(gid).segments) == _round_segs(
            oracle)
