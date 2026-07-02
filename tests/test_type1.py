"""Tests for the Adobe Type 1 (PFB) charstring interpreter."""

from pathlib import Path

import pytest

from readafp.foca import parse_fonts
from readafp.parser import iter_fields, parse_file
from readafp.type1 import Type1Font, glyph_to_path_d

TESTDATA = Path(__file__).parent.parent / "testdata"
OUTLINE = TESTDATA / "github-samples" / "afplib" / "C0X00006.afp"


def _fng(path: Path) -> bytes:
    fields = iter_fields(path.read_bytes())
    return b"".join(f.data for f in fields if f.sf_id == 0xD3EE89)


def _font() -> Type1Font:
    return Type1Font(_fng(OUTLINE))


def test_loads_charstrings_and_em() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    t1 = _font()
    assert t1.units_per_em == 1000
    assert len(t1.glyph_names) > 300
    assert t1.has_glyph("A") and t1.has_glyph("braceright")


def test_advances_match_fni_metrics() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    t1 = _font()
    # The FNI character increments are an independent oracle for the
    # charstring-derived advance widths.
    fni = {c.name: c.char_increment
           for c in parse_fonts(parse_file(str(OUTLINE)))[0].chars if c.name}
    for name in ("a", "m", "o", "f", "A", "braceright", "period"):
        glyph = t1.glyph(name)
        assert glyph is not None
        assert round(glyph.advance) == fni[name]


def test_space_glyph_has_no_outline() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    glyph = _font().glyph("space")
    assert glyph is not None and glyph.segments == []


def test_seac_composes_accented_glyph() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    t1 = _font()
    a = t1.glyph("a")
    acirc = t1.glyph("acircumflex")
    assert a and acirc
    # The composite carries the base 'a' outline plus the accent, so it has
    # strictly more segments, while keeping the base advance width.
    assert len(acirc.segments) > len(a.segments)
    assert round(acirc.advance) == round(a.advance)


def test_glyph_to_path_d_emits_svg_path() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    t1 = _font()
    a_path = glyph_to_path_d(t1.glyph("A"), scale=0.1, ox=0, oy=100)
    assert a_path.startswith("M") and "Z" in a_path  # closed subpaths
    o_path = glyph_to_path_d(t1.glyph("o"), scale=0.1, ox=0, oy=100)
    assert "C" in o_path  # the round 'o' uses cubic curves


def test_hostile_subr_index_is_capped() -> None:
    """A crafted `dup 900000000 ...` must not densify a 900M-element list."""
    from readafp.type1 import _parse_subrs

    private = (
        b"/Subrs 2 array\n"
        b"dup 0 1 RD X NP\n"
        b"dup 900000000 1 RD X NP\n"
        b"/CharStrings 0 dict\n"
    )
    subrs = _parse_subrs(private, 0)
    assert len(subrs) == 1  # the hostile index is ignored, not allocated


def test_malformed_charstrings_return_none() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    t1 = _font()
    # struct.error (truncated 255 operand) and TypeError (rrcurveto on an
    # empty stack) must degrade to None, not escape the glyph decoder.
    t1._charstrings["bad255"] = b"\xff\x00\x00"
    t1._charstrings["badcurve"] = b"\x08"
    assert t1.glyph("bad255") is None
    assert t1.glyph("badcurve") is None
