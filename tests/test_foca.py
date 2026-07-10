"""Tests for FOCA raster-font decoding and specimen rendering."""

import struct
from pathlib import Path

import pytest

from readafp.foca import (
    PATTECH_CID,
    PATTECH_RASTER,
    _fnc_resolution,
    parse_coded_fonts,
    parse_fonts,
)
from readafp.parser import iter_fields, parse_file
from readafp.ptoca import extract_pages
from readafp.render import page_to_svg

TESTDATA = Path(__file__).parent.parent / "testdata"
SAMPLE1 = TESTDATA / "Sample Files" / "Sample 1.afp"
OUTLINE = TESTDATA / "github-samples" / "afplib" / "C0X00006.afp"
FOCA_SAMPLE = TESTDATA / "foca_sample.afp"
CODED_FONT_SAMPLE = TESTDATA / "coded_font_sample.afp"


def _png_dims(png: bytes) -> tuple:
    assert png.startswith(b"\x89PNG")
    return struct.unpack(">II", png[16:24])


def test_parse_raster_fonts_from_sample1() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    fonts = parse_fonts(parse_file(str(SAMPLE1)))
    raster = [f for f in fonts if f.is_raster and f.glyphs]
    assert raster, "no raster fonts decoded"
    times = max(raster, key=lambda f: len(f.glyphs))
    assert times.pattern_tech == PATTECH_RASTER
    assert "TIMES" in times.typeface.upper()
    # Glyphs carry a GCGID, an advance and a real bitmap.
    g = next(g for g in times.glyphs if g.gcgid == "LF010000")  # 'f'
    assert g.char_increment > 0
    w, h = _png_dims(g.png)
    assert (w, h) == (g.width, g.height) and w > 1 and h > 1


def test_outline_font_has_metrics_but_no_bitmaps() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    fonts = parse_fonts(parse_file(str(OUTLINE)))
    font = fonts[0]
    assert font.is_outline and not font.is_raster
    assert font.pattern_tech == PATTECH_CID  # FNC PatTech byte X'1F'
    # The real format is read from the embedded program, not the PatTech.
    assert font.tech_label == "Adobe Type 1 (PFB) outline"
    assert font.glyphs == []  # outline shape data is not rasterized
    assert font.typeface  # descriptor name still decoded
    # FNI metrics are recovered even without glyph shapes.
    assert font.chars, "no character metrics decoded"
    assert all(c.gcgid for c in font.chars)
    assert any(c.char_increment > 0 for c in font.chars)


def test_outline_font_dedups_orientations() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    font = parse_fonts(parse_file(str(OUTLINE)))[0]
    # The FNI lists each character once per rotation; chars is collapsed
    # to one entry per GCGID and the rotation count is reported separately.
    assert font.orientations == 4
    gcgids = [c.gcgid for c in font.chars]
    assert len(gcgids) == len(set(gcgids)), "duplicate GCGIDs not collapsed"
    # The retained record is the primary (0 degrees) orientation: these
    # are the exact Helvetica per-mille advances.
    widths = {c.gcgid: c.char_increment for c in font.chars}
    assert widths["LM010000"] == 833  # 'm'
    assert widths["LO010000"] == 556  # 'o'
    assert widths["LF010000"] == 278  # 'f'


def test_outline_specimen_text_is_not_width_fitted() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    page = extract_pages(list(iter_fields(OUTLINE.read_bytes())))[0]
    # The grid is fixed-layout, so no run may carry the fitting flag and
    # the rendered SVG must not stretch any cell with textLength.
    assert all(not t.fit for t in page.texts)
    assert "textLength" not in page_to_svg(page)


def test_outline_font_resolves_glyph_names() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    font = parse_fonts(parse_file(str(OUTLINE)))[0]
    names = {c.gcgid: c.name for c in font.chars}
    # The Font Name Map gives PostScript glyph names for every character.
    assert all(c.name for c in font.chars), "some GCGIDs left unnamed"
    assert names["LA010000"] == "a"
    assert names["GA010000"] == "alpha"
    assert names["GD010000"] == "delta"


def test_outline_font_renders_glyph_specimen() -> None:
    if not OUTLINE.exists():
        pytest.skip("outline font fixture not present")
    pages = extract_pages(list(iter_fields(OUTLINE.read_bytes())))
    assert len(pages) == 1, "outline font must not render blank"
    page = pages[0]
    assert page.texts[0].text.startswith("Embedded outline font:")
    # Type 1 outlines are decoded, so the page draws actual glyph shapes
    # (one VectorGraphic per glyph) labeled by name, not a metadata table.
    assert page.graphics, "no glyph outlines drawn"
    assert any(t.font_family == "Consolas" for t in page.texts)
    svg = page_to_svg(page)
    assert "Embedded outline font" in svg
    assert svg.count("<path") == len(page.graphics) > 100


def test_typeface_name_strips_grid_suffix() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    fonts = parse_fonts(parse_file(str(SAMPLE1)))
    assert all("@" not in f.typeface for f in fonts)


def test_font_resource_renders_specimen_pages() -> None:
    if not FOCA_SAMPLE.exists():
        pytest.skip("FOCA sample not generated")
    pages = extract_pages(list(iter_fields(FOCA_SAMPLE.read_bytes())))
    assert len(pages) >= 1
    page = pages[0]
    # A specimen page is a title run plus one image per glyph.
    assert page.images
    assert len(page.texts) == len(page.images) + 1
    assert page.texts[0].text.startswith("Embedded raster font:")
    svg = page_to_svg(page)
    assert "data:image/png;base64," in svg


def test_no_fonts_yields_no_specimen() -> None:
    # A document with a real page must not be hijacked by the specimen path.
    sf = lambda i, d=b"": b"\x5a" + struct.pack(
        ">H", len(d) + 8
    ) + i.to_bytes(3, "big") + b"\x00\x00\x00" + d
    ptx = bytes.fromhex("2bd3") + bytes([3, 0xDA]) + b"\xc1"
    doc = sf(0xD3A8A8) + sf(0xD3A8AF) + sf(0xD3EE9B, ptx) + sf(
        0xD3A9AF
    ) + sf(0xD3A9A8)
    pages = extract_pages(list(iter_fields(doc)))
    assert len(pages) == 1
    assert pages[0].texts[0].text == "A"  # normal render, not a specimen


def test_describe_foca_field_decodes_metrics() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    from readafp.foca import describe_foca_field
    fields = parse_file(str(SAMPLE1))
    fnd = describe_foca_field(next(f for f in fields if f.sf_id == 0xD3A689))
    assert "FaceName=TIMES-ROMAN@0" in fnd
    assert "WeightClass=7" in fnd and "WidthClass=5" in fnd
    fnc = describe_foca_field(next(f for f in fields if f.sf_id == 0xD3A789))
    assert "MaxW=174" in fnc and "MaxH=171" in fnc
    assert "PatternsSize=15582" in fnc
    fno = describe_foca_field(next(f for f in fields if f.sf_id == 0xD3AE89))
    assert "CharRotation=0" in fno and "SpaceCharInc=250" in fno
    cpd = describe_foca_field(next(f for f in fields if f.sf_id == 0xD3A687))
    assert "NumCodePoints=6" in cpd and "GCGIDLen=8" in cpd
    cpc = describe_foca_field(next(f for f in fields if f.sf_id == 0xD3A787))
    assert "DefaultChar=SP010000" in cpc and "SpaceCharVal=64" in cpc
    cpi = describe_foca_field(next(f for f in fields if f.sf_id == 0xD38C87))
    assert "6 code points" in cpi and "SP010000" in cpi
    # FNP: the TIMES-BOLD font's position field decodes to 697/23.
    fnps = [describe_foca_field(f) for f in fields if f.sf_id == 0xD3AC89]
    assert all(fnp.startswith("MaxAscender=") for fnp in fnps)
    assert "MaxAscender=697 MaxDescender=23" in fnps


def test_raster_font_carries_resolution_pointsize_and_baseline() -> None:
    # The body font records its pattern resolution (300 dpi) and nominal
    # size (10 pt) from FNC/FND, and per-glyph baseline offsets so
    # descenders drop below the line.
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    fonts = {f.name: f for f in parse_fonts(list(iter_fields(SAMPLE1.read_bytes())))}
    body = fonts["C0AAAB00"]
    assert body.resolution == 300
    assert body.point_size == 10.0
    by_id = {g.gcgid: g for g in body.glyphs}
    # Descenders carry a positive baseline offset; an upright letter does not.
    assert by_id["LP010000"].baseline_offset > 0  # 'p'
    assert by_id["LG010000"].baseline_offset > 0  # 'g'
    assert by_id["LA010000"].baseline_offset == 0  # 'a'


def test_fnc_resolution_prefers_shape_then_metric() -> None:
    # Shape resolution (XfrUnits, bytes 24-25) wins when present.
    fnc = bytearray(26)
    struct.pack_into(">H", fnc, 6, 3000)   # XftUnits metric res 300 dpi
    struct.pack_into(">H", fnc, 24, 2400)  # XfrUnits shape res 240 dpi
    assert _fnc_resolution(bytes(fnc)) == 240
    # When the shape field is absent (short 22-byte FNC) or zero, fall back
    # to the font-metric resolution (XftUnits).
    struct.pack_into(">H", fnc, 24, 0)
    assert _fnc_resolution(bytes(fnc)) == 300
    assert _fnc_resolution(bytes(fnc[:22])) == 300


def test_parse_coded_fonts_resolves_char_set_and_code_page() -> None:
    # A coded font (BCF/CFI) binds a char set + code page; parse_coded_fonts
    # recovers both names keyed by the coded-font name.
    if not CODED_FONT_SAMPLE.exists():
        pytest.skip("coded_font_sample.afp not generated")
    coded = parse_coded_fonts(list(iter_fields(CODED_FONT_SAMPLE.read_bytes())))
    assert coded["X0TSTB00"] == ("C0AAAB00", "T1TSTB00")


def test_char_sets_carry_weight_and_unit_base() -> None:
    # foca_sample embeds a TIMES-ROMAN (WeightClass 7 = bold) and a COURIER
    # (WeightClass 5 = medium), both relative-metric (increments in 1000/em)
    # at 300 dpi — the metrics the coded-font resolution relies on.
    fonts = {f.name: f for f in parse_fonts(list(iter_fields(
        FOCA_SAMPLE.read_bytes())))}
    assert fonts["C0AAAB00"].weight_class == 7
    assert fonts["C0AAAD00"].weight_class == 5
    assert fonts["C0AAAB00"].relative_metrics is True
    assert fonts["C0AAAB00"].resolution == 300


def test_coded_font_document_uses_embedded_font_family() -> None:
    # The regression this change fixes: a document that maps its font through
    # a coded-font name (MCF FQN X'8E') — here by a rotation-selector name
    # (X1…) whose resource is embedded as X0… — must resolve to the embedded
    # char set's face (TIMES-ROMAN -> serif), not fall back to bare "Arial".
    if not CODED_FONT_SAMPLE.exists():
        pytest.skip("coded_font_sample.afp not generated")
    fields = list(iter_fields(CODED_FONT_SAMPLE.read_bytes()))
    runs = [t for p in extract_pages(fields) for t in p.texts]
    assert runs, "no text runs"
    assert all(t.font_family == "Times New Roman, serif" for t in runs)
    assert all(t.font_family != "Arial" for t in runs)  # not the bare default
    # With small fonts enabled the run draws in the file's own raster glyphs.
    imgs = [im for p in extract_pages(fields, embed_small_fonts=True)
            for im in p.images]
    assert imgs, "embedded glyphs not drawn"
