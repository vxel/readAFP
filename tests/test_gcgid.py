"""Tests for the external-code-page -> embedded-glyph bridge (gcgid.py)."""

from pathlib import Path

from readafp import gcgid
from readafp.foca import parse_fonts
from readafp.parser import iter_fields

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
SAMPLE1 = TESTDATA / "Sample Files" / "Sample 1.afp"


def test_gcgid_for_char_letters() -> None:
    assert gcgid.gcgid_for_char("A") == "LA010000"
    assert gcgid.gcgid_for_char("Z") == "LZ010000"
    assert gcgid.gcgid_for_char("a") == "LA020000"
    assert gcgid.gcgid_for_char("z") == "LZ020000"


def test_gcgid_for_char_digits() -> None:
    assert gcgid.gcgid_for_char("1") == "ND010000"
    assert gcgid.gcgid_for_char("9") == "ND090000"
    assert gcgid.gcgid_for_char("0") == "ND100000"


def test_gcgid_for_char_space_and_unicode() -> None:
    assert gcgid.gcgid_for_char(" ") == "SP010000"
    # Algorithmic UNICxxxx for anything else in the BMP.
    assert gcgid.gcgid_for_char("—") == "UNIC2014"  # em dash
    assert gcgid.gcgid_for_char("ﬁ") == "UNICFB01"  # fi ligature
    assert gcgid.gcgid_for_char("â") == "UNIC00E2"  # a-circumflex


def test_gcgid_for_char_rejects_bad_input() -> None:
    assert gcgid.gcgid_for_char("") is None
    assert gcgid.gcgid_for_char("AB") is None


def test_bridge_only_keeps_glyphs_the_font_has() -> None:
    """A byte maps only when its derived GCGID is in the font's glyph set."""
    font_gcgids = {"LA010000", "LA020000", "SP010000"}  # A, a, space only
    cp_map = gcgid.bridge_code_page("cp500", font_gcgids)
    # cp500: 0xC1='A', 0x81='a', 0x40=space all map; others drop.
    assert cp_map[0xC1] == "LA010000"
    assert cp_map[0x81] == "LA020000"
    assert cp_map[0x40] == "SP010000"
    assert 0xC2 not in cp_map  # 'B' has no glyph here, so not mapped
    # Never maps a byte to a glyph the font lacks.
    assert set(cp_map.values()) <= font_gcgids


def test_bridge_empty_when_no_glyphs() -> None:
    assert gcgid.bridge_code_page("cp500", set()) == {}


def test_bridge_unknown_codec_is_safe() -> None:
    assert gcgid.bridge_code_page("no-such-codec", {"LA010000"}) == {}


def test_bridge_matches_sample1_embedded_font_exactly() -> None:
    """The cp1140 bridge maps a byte iff its GCGID is a glyph the font has.

    Sample 1's fonts are subsetted (only glyphs the document uses), so the
    bridge must map exactly the present letters and never invent the rest —
    the core fidelity guarantee.
    """
    if not SAMPLE1.exists():
        import pytest

        pytest.skip("Sample 1 not present")
    fonts = {f.name: f for f in parse_fonts(list(iter_fields(SAMPLE1.read_bytes())))}
    body = fonts["C0AAAB00"]  # the 64-glyph TIMES-ROMAN body font
    gset = {g.gcgid for g in body.glyphs}
    cp_map = gcgid.bridge_code_page("cp1140", gset)
    # Exact invariant: byte present iff its derived GCGID is in the font.
    for byte in range(256):
        ch = bytes([byte]).decode("cp1140")
        derived = gcgid.gcgid_for_char(ch)
        if derived in gset:
            assert cp_map.get(byte) == derived
        else:
            assert byte not in cp_map
    # Letters the font does carry are recovered (e.g. 'A' and 'o').
    assert cp_map[b"A".decode("ascii").encode("cp1140")[0]] == "LA010000"
    assert "LA010000" in gset and "LO020000" in gset
