"""Tests for the external-code-page -> embedded-glyph bridge (gcgid.py)."""

from pathlib import Path

from readafp import gcgid
from readafp.foca import parse_fonts
from readafp.parser import iter_fields

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
SAMPLE1 = TESTDATA / "Sample Files" / "Sample 1.afp"


def test_gcgid_for_char_letters() -> None:
    # CS103 (FOCA Fig. 56): lowercase is L*010000, uppercase L*020000.
    assert gcgid.gcgid_for_char("a") == "LA010000"
    assert gcgid.gcgid_for_char("z") == "LZ010000"
    assert gcgid.gcgid_for_char("A") == "LA020000"
    assert gcgid.gcgid_for_char("Z") == "LZ020000"


def test_gcgid_for_char_digits() -> None:
    assert gcgid.gcgid_for_char("1") == "ND010000"
    assert gcgid.gcgid_for_char("9") == "ND090000"
    assert gcgid.gcgid_for_char("0") == "ND100000"


def test_gcgid_for_char_punctuation() -> None:
    # Punctuation from the verified Figure 56 table.
    assert gcgid.gcgid_for_char(" ") == "SP010000"
    assert gcgid.gcgid_for_char(".") == "SP110000"
    assert gcgid.gcgid_for_char(",") == "SP080000"
    assert gcgid.gcgid_for_char("/") == "SP120000"
    assert gcgid.gcgid_for_char("(") == "SP060000"
    assert gcgid.gcgid_for_char("@") == "SM050000"


def test_gcgid_for_char_unicode_fallback() -> None:
    # Outside CS103: algorithmic UNICxxxx (some fonts key glyphs this way).
    assert gcgid.gcgid_for_char("—") == "UNIC2014"  # em dash
    assert gcgid.gcgid_for_char("ﬁ") == "UNICFB01"  # fi ligature
    assert gcgid.gcgid_for_char("â") == "UNIC00E2"  # a-circumflex


def test_gcgid_for_char_rejects_bad_input() -> None:
    assert gcgid.gcgid_for_char("") is None
    assert gcgid.gcgid_for_char("AB") is None


def test_cs103_table_matches_cp500_codec() -> None:
    """Every CS103 cell's character must match the cp500 codec (integrity)."""
    for byte, g in gcgid._CS103_CP500.items():
        ch = bytes([byte]).decode("cp500")
        assert gcgid.gcgid_for_char(ch) == g
    assert len(gcgid._CS103_CP500) == 95  # full Figure 56 transcription


def test_bridge_only_keeps_glyphs_the_font_has() -> None:
    """A byte maps only when its derived GCGID is in the font's glyph set."""
    font_gcgids = {"LA010000", "LA020000", "SP010000"}  # a, A, space only
    cp_map = gcgid.bridge_code_page("cp500", font_gcgids)
    # cp500: 0xC1='A'->LA020000, 0x81='a'->LA010000, 0x40=space->SP010000.
    assert cp_map[0xC1] == "LA020000"
    assert cp_map[0x81] == "LA010000"
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
    bridge must map exactly the present characters and never invent the
    rest — the core fidelity guarantee.
    """
    if not SAMPLE1.exists():
        import pytest

        pytest.skip("Sample 1 not present")
    fonts = {
        f.name: f for f in parse_fonts(list(iter_fields(SAMPLE1.read_bytes())))
    }
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
    # Real letters resolve to the correctly-cased glyph the font carries.
    assert cp_map[ord("A".encode("cp1140"))] == "LA020000"  # uppercase
    assert cp_map[ord("a".encode("cp1140"))] == "LA010000"  # lowercase
