"""Tests for PTOCA decoding, page extraction and SVG rendering."""

from pathlib import Path

import pytest

from readafp.parser import iter_fields, parse_file
from readafp.ptoca import (
    ControlSequence,
    ImageRef,
    Page,
    extract_pages,
    iter_control_sequences,
    parse_mdr_fonts,
    _EmbeddedFont,
    _TextState,
    _coded_font_point_size,
    _decode_trn,
)
from readafp.foca import Glyph, _glyph_png
from readafp.render import _glyph_ink_id, page_to_svg

TESTDATA = Path(__file__).parent.parent / "testdata"
HEALTH_SAMPLE = TESTDATA / "sample1_health" / "01_Health_Coverage.afp"
SIMPLE_PAIR = TESTDATA / "fop-pairs" / "simple.afp"

# Escape, then a chain: AMI(0x0064) chained -> AMB(0x00C8) chained ->
# TRN("AB" in EBCDIC) unchained.
CHAIN = bytes.fromhex("2bd3" "04c70064" "04d300c8" "04dac1c2")


def test_iter_control_sequences_follows_chaining() -> None:
    seqs = list(iter_control_sequences(CHAIN))
    assert [s.cs_type for s in seqs] == [0xC6, 0xD2, 0xDA]
    assert seqs[0].params == bytes.fromhex("0064")


def test_unchained_sequence_requires_new_escape() -> None:
    # TRN unchained, followed by garbage that is not an escape: stop.
    data = bytes.fromhex("2bd3" "04dac1c2" "04c70064")
    seqs = list(iter_control_sequences(data))
    assert len(seqs) == 1
    assert seqs[0].cs_type == 0xDA


def test_decode_trn_utf16be() -> None:
    assert _decode_trn("John".encode("utf-16-be")) == "John"


def test_decode_trn_ebcdic() -> None:
    assert _decode_trn("John".encode("cp500")) == "John"


def test_control_sequence_names() -> None:
    assert "TRN" in ControlSequence(cs_type=0xDA, params=b"").name
    assert "Unknown" in ControlSequence(cs_type=0x10, params=b"").name


def test_svi_sets_space_width_on_run() -> None:
    # SVI(0x0014=20) chained -> TRN("A B" in EBCDIC) unchained.
    data = bytes.fromhex("2bd3" "04c50014" "05dac140c2")
    page = Page(units_per_inch=240)
    state = _TextState()
    for cs in iter_control_sequences(data):
        state.apply(cs, page)
    assert len(page.texts) == 1
    run = page.texts[0]
    assert run.text == "A B"
    # The producer's variable-space increment rides on the run so the
    # renderer can widen its spaces (justification).
    assert run.space_width == 20
    assert state.space_increment == 20


def test_trn_without_svi_has_no_space_width() -> None:
    # TRN("A B") with no preceding SVI: spaces fall back to the font default.
    data = bytes.fromhex("2bd3" "05dac140c2")
    page = Page(units_per_inch=240)
    state = _TextState()
    for cs in iter_control_sequences(data):
        state.apply(cs, page)
    assert page.texts[0].space_width is None


def _dropped_note(run) -> bool:
    return any("couldn't encode" in n.msg for n in run.notes)


def test_dropped_glyph_run_is_flagged() -> None:
    # TRN of all X'3F' (EBCDIC SUBSTITUTE) separated by EBCDIC spaces: the
    # producer dropped these glyphs (no AFP font), e.g. a ZapfDingbats row.
    data = bytes.fromhex("2bd3" "0ada" "3f403f403f403f40")
    page = Page(units_per_inch=240)
    state = _TextState()
    for cs in iter_control_sequences(data):
        state.apply(cs, page)
    assert page.texts and _dropped_note(page.texts[0])


def test_lone_substitute_is_not_flagged() -> None:
    # A single X'3F' amid real text is FOP's list bullet, not a dropped run.
    data = bytes.fromhex("2bd3" "08da" "c1403f40c240")  # "A • B"
    page = Page(units_per_inch=240)
    state = _TextState()
    for cs in iter_control_sequences(data):
        state.apply(cs, page)
    assert page.texts and not _dropped_note(page.texts[0])


def _raster_emb_font() -> _EmbeddedFont:
    """A minimal embedded raster font: one all-black 8x8 glyph for byte 'A'."""
    png = _glyph_png(b"\xff" * 8, 8, 8)  # all pels toned -> all-black bitmap
    glyph = Glyph(gcgid="LA010000", width=8, height=8, char_increment=500,
                  png=png, baseline_offset=0)
    return _EmbeddedFont(
        cp_map={0x41: "LA010000"},
        glyphs={"LA010000": glyph},
        ref_height=8,
        resolution=300,
        point_size=24.0,  # above the 20pt display gate
    )


def test_embedded_glyph_applies_stc_color() -> None:
    # A non-default STC/SEC color recolors the 1-bit glyph bitmap; the run's
    # decoded text is still recorded for export.
    emb = _raster_emb_font()
    page = Page(units_per_inch=240)
    state = _TextState()
    state.color = "#ff0000"
    assert state._emit_embedded_glyphs(page, b"\x41", emb, 80) is True
    assert page.images and page.images[-1].recolor == "#ff0000"


def test_embedded_glyph_black_is_not_recolored() -> None:
    # Default black needs no filter — the bitmap is already black-on-white.
    emb = _raster_emb_font()
    page = Page(units_per_inch=240)
    state = _TextState()  # color defaults to black
    assert state._emit_embedded_glyphs(page, b"\x41", emb, 80) is True
    assert page.images and page.images[-1].recolor is None


def test_page_to_svg_recolors_glyph_bitmap() -> None:
    # A recolored glyph image gets a flood/composite filter keyed to its color;
    # a plain image (recolor None) is left untouched.
    png = _glyph_png(b"\xff" * 8, 8, 8)
    page = Page(units_per_inch=240)
    page.images.append(ImageRef(x=0, y=0, width=80, height=80, mime="image/png",
                                data=png, crisp=True, recolor="#ff0000"))
    page.images.append(ImageRef(x=0, y=0, width=80, height=80, mime="image/png",
                                data=png, crisp=True))
    svg = page_to_svg(page)
    fid = _glyph_ink_id("#ff0000")
    assert f'<filter id="{fid}"' in svg
    assert 'flood-color="#ff0000"' in svg
    assert f'filter="url(#{fid})"' in svg
    # Exactly one image references the filter (the recolored one).
    assert svg.count(f'filter="url(#{fid})"') == 1


def test_embedded_glyph_rotation_sets_transform() -> None:
    # A non-zero STO orientation tags each glyph image with a page-space
    # rotation pivoted at the run origin; 90/270 swap the I/B scalars into
    # page coords (pivot = (b, i)), matching the substitute-text path.
    emb = _raster_emb_font()
    page = Page(units_per_inch=240)
    st = _TextState()
    st.orientation = 90
    st.i, st.b = 300, 400
    assert st._emit_embedded_glyphs(page, b"\x41", emb, 80) is True
    assert page.images[-1].rotate == (90, 400, 300)


def test_embedded_glyph_upright_has_no_rotation() -> None:
    emb = _raster_emb_font()
    page = Page(units_per_inch=240)
    st = _TextState()  # orientation defaults to 0
    st.i, st.b = 300, 400
    st._emit_embedded_glyphs(page, b"\x41", emb, 80)
    assert page.images[-1].rotate is None


def test_page_to_svg_emits_rotate_transform() -> None:
    png = _glyph_png(b"\xff" * 8, 8, 8)
    page = Page(units_per_inch=240)
    page.images.append(ImageRef(x=0, y=0, width=80, height=80, mime="image/png",
                                data=png, rotate=(90, 400, 300)))
    assert 'transform="rotate(90 400 300)"' in page_to_svg(page)


def test_coded_font_point_size_from_name() -> None:
    # IBM/FOP char-set name: 7th char encodes the size (B=12 … H=18, '0'=10).
    assert _coded_font_point_size("C0H200B0") == 12
    assert _coded_font_point_size("C0H200F0") == 16
    assert _coded_font_point_size("C0H200H0") == 18
    assert _coded_font_point_size("C0H20000") == 10
    assert _coded_font_point_size("C0H400D0") == 14  # weight digit ignored
    assert _coded_font_point_size("Arial") is None


def test_external_font_size_decoded_from_name() -> None:
    if not (TESTDATA / "fop-pairs" / "table.afp").exists():
        pytest.skip("fop-pairs not present")
    page = extract_pages(parse_file(
        str(TESTDATA / "fop-pairs" / "table.afp")))[0]
    pt = [round(t.font_size / page.units_per_inch * 72) for t in page.texts]
    # Title 18pt, "A simple table" 16pt — not the 12pt body default.
    assert pt[0] == 18 and pt[1] == 16


def test_svi_justifies_simple_fop_pair() -> None:
    if not SIMPLE_PAIR.exists():
        pytest.skip("fop-pairs not present")
    pages = extract_pages(parse_file(str(SIMPLE_PAIR)))
    page = pages[0]
    # FOP varies the SVI per line to justify: the first body line is wide
    # (20) and a paragraph's last line drops to the natural minimum (7).
    widths = {t.space_width for t in page.texts if t.space_width}
    assert 20 in widths and 7 in widths
    assert "word-spacing" in page_to_svg(page)


def test_extract_pages_health_sample() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    pages = extract_pages(parse_file(str(HEALTH_SAMPLE)))
    assert len(pages) == 1
    page = pages[0]
    # Letter at 1440 units/inch, from the PGD.
    assert (page.width, page.height) == (12240, 15840)
    assert page.units_per_inch == 1440
    text = page.plain_text
    assert "John" in text and "Doe" in text
    assert "Health" in text
    # The first PTX draws the page border rules.
    assert page.rules


def test_health_sample_heading_is_colored() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    colors = {run.color for run in page.texts}
    assert len(colors) > 1  # heading color differs from body text


def test_page_to_svg_escapes_and_positions() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    svg = page_to_svg(page)
    assert svg.startswith("<svg")
    assert 'viewBox="0 0 12240 15840"' in svg
    assert 'data-upi="1440"' in svg  # zoom/X,Y readout need real units
    assert "John" in svg
    assert "&" not in svg.replace("&amp;", "").replace("&lt;", "").replace(
        "&gt;", ""
    ).replace("&quot;", "").replace("&#", "")


def test_mdr_font_mapping_health_sample() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    fields = parse_file(str(HEALTH_SAMPLE))
    mdr = next(f for f in fields if f.sf_id == 0xD3ABC3)
    fonts = parse_mdr_fonts(mdr.data)
    assert fonts[1].family == "Arial" and fonts[1].weight == "bold"
    assert fonts[2].family == "Segoe UI"
    assert fonts[3].family == "Arial" and fonts[3].weight == "normal"
    # 9pt and 27pt, in L-units (1pt = 20 units at 1440/inch).
    assert fonts[1].size == 180
    assert fonts[2].size == 540


def test_text_runs_carry_mapped_fonts() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    john = next(r for r in page.texts if r.text == "John")
    assert john.font_weight == "bold"
    assert john.font_size == 180
    heading = next(r for r in page.texts if "Continuing" in r.text)
    assert heading.font_family == "Segoe UI"
    assert heading.font_size == 540


def test_table_band_sits_behind_white_labels() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    options = next(r for r in page.texts if r.text == "Options")
    assert options.color == "#ffffff"
    band = [
        r
        for r in page.rules
        if r.color == "#2196f3" and r.axis == "I" and r.thickness >= 100
    ]
    assert band, "blue header band rules missing"
    # Rules extend downward from their position: the white label's
    # baseline must fall inside the band's vertical span.
    cell = band[0]
    assert cell.y <= options.y <= cell.y + cell.thickness


def test_object_container_jpeg_is_placed() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    assert len(page.images) == 1
    logo = page.images[0]
    assert logo.mime == "image/jpeg"
    assert logo.data.startswith(b"\xff\xd8\xff")
    assert (logo.x, logo.y) == (8715, 975)
    assert (logo.width, logo.height) == (2385, 720)
    svg = page_to_svg(page)
    assert "data:image/jpeg;base64," in svg


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    """Build one structured-field record."""
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + (len(body) + 2).to_bytes(2, "big") + body


def test_extract_pages_multipage_document() -> None:
    def page(text: str) -> bytes:
        ptx = bytes.fromhex("2bd3" "04c70064" "04d300c8") + bytes(
            [2 + len(text), 0xDA]
        ) + text.encode("cp500")
        return (
            _sf(0xD3A8AF, b"\x00" * 8)  # BPG
            + _sf(0xD3EE9B, ptx)  # PTX
            + _sf(0xD3A9AF, b"\x00" * 8)  # EPG
        )

    doc = (
        _sf(0xD3A8A8, b"\x00" * 8)
        + page("First")
        + page("Second")
        + _sf(0xD3A9A8, b"\x00" * 8)
    )
    pages = extract_pages(list(iter_fields(doc)))
    assert len(pages) == 2
    assert pages[0].texts[0].text == "First"
    assert pages[1].texts[0].text == "Second"
    assert pages[1].texts[0].x == 0x64 and pages[1].texts[0].y == 0xC8


def test_unbracketed_ptx_lands_on_implicit_page() -> None:
    ptx = bytes.fromhex("2bd3" "04c70064" "04d300c8") + bytes(
        [2 + 5, 0xDA]
    ) + "Loose".encode("cp500")
    doc = (
        _sf(0xD3A8A8, b"\x00" * 8)
        + _sf(0xD3EE9B, ptx)
        + _sf(0xD3A9A8, b"\x00" * 8)
    )
    pages = extract_pages(list(iter_fields(doc)))
    assert len(pages) == 1
    assert pages[0].texts[0].text == "Loose"


def test_codepage_override_decodes_ibm273() -> None:
    sample = TESTDATA / "alpheus-corpus" / "large_ibm273.afp"
    if not sample.exists():
        pytest.skip("test corpus not present")
    fields = parse_file(str(sample))
    # The fixture's text is German: "Hällö Wörld" in cp273. cp500 maps
    # those bytes to {/¦ instead.
    garbled = extract_pages(fields, codepage="cp500")[0].texts[0].text
    assert "H{ll" in garbled
    readable = extract_pages(fields, codepage="cp273")[0].texts[0].text
    assert "Hällö Wörld" in readable


def test_implicit_page_corpus_large_ibm273() -> None:
    sample = TESTDATA / "alpheus-corpus" / "large_ibm273.afp"
    if not sample.exists():
        pytest.skip("test corpus not present")
    pages = extract_pages(parse_file(str(sample)))
    # Unpositioned text flows, wraps at the page width and paginates.
    assert len(pages) > 1
    assert all(p.width == 12240 for p in pages)
    assert pages[0].texts
    assert all(0 <= r.y <= pages[0].height for r in pages[0].texts)


def test_run_cap_marks_page_truncated() -> None:
    from readafp.ptoca import MAX_RUNS_PER_PAGE

    one_trn = bytes([2 + 2, 0xDB]) + "AB".encode("cp500")  # chained TRN
    ptx = bytes.fromhex("2bd3") + one_trn * (MAX_RUNS_PER_PAGE + 10)
    doc = (
        _sf(0xD3A8A8, b"\x00" * 8)
        + _sf(0xD3EE9B, ptx)
        + _sf(0xD3A9A8, b"\x00" * 8)
    )
    pages = extract_pages(list(iter_fields(doc)))
    assert sum(len(p.texts) for p in pages) == MAX_RUNS_PER_PAGE
    assert pages[-1].truncated
    assert "[render truncated" in page_to_svg(pages[-1])


def test_runs_carry_source_field_offset() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    page = extract_pages(parse_file(str(HEALTH_SAMPLE)))[0]
    # All text comes from the second PTX (offset 6559); the border and
    # band rules come from the first (offset 6039).
    assert {r.src for r in page.texts} == {6559}
    assert 6039 in {r.src for r in page.rules}
    svg = page_to_svg(page)
    assert 'data-src="6559"' in svg and 'data-src="6039"' in svg


def test_fop_pair_font_sizes_from_baseline_pitch() -> None:
    sample = TESTDATA / "fop-pairs" / "table.afp"
    if not sample.exists():
        pytest.skip("FOP pairs not present")
    pages = extract_pages(parse_file(str(sample)))
    assert len(pages) == 7
    page = pages[0]
    # FOP emits 240 units/inch; 12pt is 40 units there. The body font
    # has no declared size — baseline pitch (48) must yield ~40, not
    # the 80+ that column-gap estimation used to produce.
    assert page.units_per_inch == 240
    body = next(r for r in page.texts if "normal text" in r.text)
    assert 30 <= body.font_size <= 48


def _ebc(s: str) -> bytes:
    return s.encode("cp500").ljust(8, b"\x40")


def _ipo_bytes(name: str, x: int, y: int) -> bytes:
    return _ebc(name) + x.to_bytes(3, "big", signed=True) + y.to_bytes(
        3, "big", signed=True
    )


def test_page_overlay_composited_with_offset() -> None:
    # Overlay LETTERHD draws "HEAD" at (700, 800); the page includes it
    # via IPO at offset (100, 200) and adds its own "BODY".
    ov = (
        bytes.fromhex("2bd3")
        + bytes([4, 0xC7]) + b"\x02\xbc"          # AMI 700
        + bytes([4, 0xD3]) + b"\x03\x20"          # AMB 800
        + bytes([2 + 4, 0xDA]) + "HEAD".encode("cp500")
    )
    body = (
        bytes.fromhex("2bd3")
        + bytes([4, 0xC7]) + b"\x07\xd0"          # AMI 2000
        + bytes([4, 0xD3]) + b"\x0b\xb8"          # AMB 3000
        + bytes([2 + 4, 0xDA]) + "BODY".encode("cp500")
    )
    doc = (
        _sf(0xD3A8A8)
        + _sf(0xD3A8DF, _ebc("LETTERHD"))         # BMO
        + _sf(0xD3EE9B, ov)                       # overlay PTX
        + _sf(0xD3A9DF, _ebc("LETTERHD"))         # EMO
        + _sf(0xD3A8AF)                           # BPG
        + _sf(0xD3AFD8, _ipo_bytes("LETTERHD", 100, 200))  # IPO
        + _sf(0xD3EE9B, body)                     # page PTX
        + _sf(0xD3A9AF)                           # EPG
        + _sf(0xD3A9A8)
    )
    pages = extract_pages(list(iter_fields(doc)))
    assert len(pages) == 1  # overlay must not become its own page
    page = pages[0]
    texts = {t.text: (t.x, t.y) for t in page.texts}
    assert texts["BODY"] == (2000, 3000)
    assert texts["HEAD"] == (800, 1000)  # 700+100, 800+200


def test_unincluded_overlay_renders_nothing() -> None:
    # An overlay defined but never referenced by an IPO contributes no page
    # when it sits inside a real document (one that has a BDT).
    ov = bytes.fromhex("2bd3") + bytes([2 + 2, 0xDA]) + "HI".encode("cp500")
    doc = (
        _sf(0xD3A8A8)
        + _sf(0xD3A8DF, _ebc("ORPHAN"))
        + _sf(0xD3EE9B, ov)
        + _sf(0xD3A9DF, _ebc("ORPHAN"))
        + _sf(0xD3A9A8)
    )
    assert extract_pages(list(iter_fields(doc))) == []


def test_standalone_overlay_resource_renders_as_page() -> None:
    # A bare overlay *resource* (no BDT, no IPO) is shown as its own page,
    # the way an AFP viewer opens a stand-alone overlay.
    ov = (
        bytes.fromhex("2bd3")
        + bytes([4, 0xC7]) + b"\x02\xbc"          # AMI 700
        + bytes([4, 0xD3]) + b"\x03\x20"          # AMB 800
        + bytes([2 + 4, 0xDA]) + "HERE".encode("cp500")
    )
    res = (
        _sf(0xD3A8DF, _ebc("STANDALN"))           # BMO (no enclosing BDT)
        + _sf(0xD3EE9B, ov)                       # overlay PTX
        + _sf(0xD3A9DF, _ebc("STANDALN"))         # EMO
    )
    pages = extract_pages(list(iter_fields(res)))
    assert len(pages) == 1
    assert "HERE" in pages[0].plain_text


def test_cs_afp_overlay_resource_renders() -> None:
    sample = TESTDATA / "github-samples" / "afplib" / "cs.afp"
    if not sample.exists():
        pytest.skip("cs.afp sample not present")
    pages = extract_pages(parse_file(str(sample)))
    assert len(pages) == 1
    assert "Simplify" in pages[0].plain_text


def test_real_overlay_text_lands_on_page() -> None:
    sample = TESTDATA / "alpheus-corpus" / "external" / "afplib_ende.afp"
    if not sample.exists():
        pytest.skip("afplib sample not present")
    pages = extract_pages(parse_file(str(sample)))
    # The included overlay carries text; it must appear on a real page,
    # not leak onto spurious implicit pages.
    assert len(pages) == 1
    assert pages[0].texts and "ENDE" in pages[0].plain_text


def test_extract_pages_empty_document() -> None:
    sample = TESTDATA / "alpheus-corpus" / "minimal.afp"
    if not sample.exists():
        pytest.skip("test corpus not present")
    assert extract_pages(parse_file(str(sample))) == []


# ---------------------------------------------------------------------------
# STO (Set Text Orientation) tests
# ---------------------------------------------------------------------------

def _sto_doc(inlorent: int) -> bytes:
    """One-page AFP with STO(inlorent) + AMI(100) + AMB(200) + TRN('A')."""
    ptx = (
        bytes.fromhex("2bd3")           # escape
        + bytes([6, 0xF7])              # STO chained, length=6
        + inlorent.to_bytes(2, "big")   # INLORENT
        + b"\x00\x00"                   # REFORNT (ignored for now)
        + bytes([4, 0xC7])              # AMI chained, length=4
        + b"\x00\x64"                   # inline pos = 100
        + bytes([4, 0xD3])              # AMB chained, length=4
        + b"\x00\xc8"                   # baseline pos = 200
        + bytes([3, 0xDA])              # TRN unchained, length=3
        + b"\xc1"                       # 'A' in cp500
    )
    return _sf(0xD3A8AF) + _sf(0xD3EE9B, ptx) + _sf(0xD3A9AF)


def test_sto_zero_is_normal_coords() -> None:
    pages = extract_pages(list(iter_fields(_sto_doc(0))))
    run = pages[0].texts[0]
    assert run.orientation == 0
    assert (run.x, run.y) == (100, 200)  # i→x, b→y unchanged


def test_sto_90_swaps_and_rotates() -> None:
    # INLORENT=11520 = 90×128 → inline goes down
    pages = extract_pages(list(iter_fields(_sto_doc(11520))))
    run = pages[0].texts[0]
    assert run.orientation == 90
    assert (run.x, run.y) == (200, 100)  # b→x, i→y for 90°/270°


def test_sto_180_no_coord_swap() -> None:
    # INLORENT=23040 = 180×128 → inline goes left
    pages = extract_pages(list(iter_fields(_sto_doc(23040))))
    run = pages[0].texts[0]
    assert run.orientation == 180
    assert (run.x, run.y) == (100, 200)  # no swap for 0°/180°


def test_sto_270_swaps_and_rotates() -> None:
    # INLORENT=34560 = 270×128 → inline goes up
    pages = extract_pages(list(iter_fields(_sto_doc(34560))))
    run = pages[0].texts[0]
    assert run.orientation == 270
    assert (run.x, run.y) == (200, 100)  # b→x, i→y for 90°/270°


def test_sto_rotation_in_svg() -> None:
    pages = extract_pages(list(iter_fields(_sto_doc(11520))))
    svg = page_to_svg(pages[0])
    assert 'transform="rotate(90,200,100)"' in svg


def test_sto_zero_no_transform_in_svg() -> None:
    pages = extract_pages(list(iter_fields(_sto_doc(0))))
    svg = page_to_svg(pages[0])
    assert "transform=" not in svg


def test_sto_180_runs_skip_textlength_fit() -> None:
    # Two runs on the same baseline at 180°: same y, increasing x — the
    # condition that would otherwise trigger _fit's textLength stretch.
    # Rotated runs must skip it (textLength is a horizontal-only metric).
    ptx = (
        bytes.fromhex("2bd3")
        + bytes([6, 0xF7]) + (23040).to_bytes(2, "big") + b"\x00\x00"  # STO 180
        + bytes([4, 0xC7]) + b"\x00\x64"          # AMI(100)
        + bytes([4, 0xD3]) + b"\x00\xc8"          # AMB(200)
        + bytes([7, 0xDB]) + "Hello".encode("cp500")  # TRN chained
        + bytes([4, 0xC7]) + b"\x03\xe8"          # AMI(1000)
        + bytes([7, 0xDA]) + "World".encode("cp500")  # TRN unchained
    )
    doc = _sf(0xD3A8AF) + _sf(0xD3EE9B, ptx) + _sf(0xD3A9AF)
    pages = extract_pages(list(iter_fields(doc)))
    runs = pages[0].texts
    assert [r.orientation for r in runs] == [180, 180]
    assert runs[0].y == runs[1].y and runs[1].x > runs[0].x  # _fit's trigger
    svg = page_to_svg(pages[0])
    assert "textLength" not in svg  # must be skipped for rotated text
    assert 'transform="rotate(180' in svg


def test_sto_resets_position() -> None:
    # After STO, i and b reset to 0; any move before STO doesn't carry over.
    ptx = (
        bytes.fromhex("2bd3")
        + bytes([4, 0xC7]) + b"\x05\xdc"  # AMI(1500) chained
        + bytes([4, 0xD3]) + b"\x07\xd0"  # AMB(2000) chained
        + bytes([6, 0xF7]) + b"\x00\x00\x00\x00"  # STO(0) chained → resets i,b
        + bytes([4, 0xC7]) + b"\x00\x64"   # AMI(100) chained
        + bytes([4, 0xD3]) + b"\x00\xc8"   # AMB(200) chained
        + bytes([3, 0xDA]) + b"\xc1"        # TRN('A') unchained
    )
    doc = _sf(0xD3A8AF) + _sf(0xD3EE9B, ptx) + _sf(0xD3A9AF)
    pages = extract_pages(list(iter_fields(doc)))
    run = pages[0].texts[0]
    assert (run.x, run.y) == (100, 200)  # pre-STO moves discarded


def test_embedded_raster_font_text_renders_as_glyphs() -> None:
    # Sample 1.afp embeds its raster character sets. Display-size text (the
    # 60pt title, a 28pt heading) is drawn in the file's own glyphs — the
    # external cp1140 code page is bridged byte->GCGID via readafp.gcgid.
    # Small body fonts (10pt) stay as a substitute font because 1-bit
    # bitmaps look rough scaled down (the _EMBED_MIN_POINT_SIZE gate).
    sample = TESTDATA / "Sample Files" / "Sample 1.afp"
    if not sample.exists():
        pytest.skip("Sample 1 not present")
    page = extract_pages(parse_file(str(sample)))[0]
    glyph_imgs = [im for im in page.images if im.crisp]
    # Only the large title/heading glyphs render as bitmaps, not the body.
    assert 0 < len(glyph_imgs) < 50
    assert all(im.data.startswith(b"\x89PNG") for im in glyph_imgs)
    # The large embedded glyphs are sizeable (display fonts), well over the
    # tiny boxes a 10pt body font would produce.
    assert max(im.height for im in glyph_imgs) > page.units_per_inch // 4
    # The bulk body text falls back to many substitute runs.
    assert len(page.texts) > 100


def test_embedded_glyph_runs_keep_extractable_text() -> None:
    # Runs drawn as embedded glyph bitmaps must still contribute to the
    # page's plain text (Copy-text / .txt export), via the hidden text
    # layer keyed on the run's code-page codec.
    sample = TESTDATA / "Sample Files" / "Sample 1.afp"
    if not sample.exists():
        pytest.skip("Sample 1 not present")
    page = extract_pages(parse_file(str(sample)))[0]
    assert page.text_layer  # glyph-drawn runs recorded their decoded text
    assert "groff" in page.plain_text.lower()


def test_decode_trn_strips_undecodable_control_chars() -> None:
    # Bytes a code page can't map to a glyph decode to control chars (e.g.
    # FOP's list bullet X'3F' -> U+001A in cp500). Render them as nothing,
    # not a tofu box.
    from readafp.ptoca import _decode_trn, _strip_controls
    assert _strip_controls("a\x1ab\x00c\x9fd") == "abcd"
    assert _strip_controls("keep\ttab\nand space") == "keep\ttab\nand space"
    assert _strip_controls("plain text") == "plain text"
    # FOP encodes its list bullet at X'3F' (cp500 -> U+001A); render as "•".
    assert _decode_trn(b"\x3f", "cp500") == "•"


def test_fit_scales_glyphs_not_gaps() -> None:
    # A fitted run uses spacingAndGlyphs so a substitute font reads as wider,
    # not as letters spread apart (the textdeko over-stretch fix).
    from readafp.render import _fit
    from readafp.ptoca import TextRun
    runs = [TextRun(x=0, y=100, text="Hello world", font_size=40),
            TextRun(x=240, y=100, text=".", font_size=40)]
    out = _fit(runs, 0)
    assert "spacingAndGlyphs" in out and "textLength" in out


def test_coded_font_name_infers_typeface() -> None:
    # IBM/FOP coded-font names: C0H=Helvetica, C04=Courier, C0N=Times.
    from readafp.ptoca import _coded_font_substitute
    assert _coded_font_substitute("C04200B0")[0].startswith("Courier")
    assert _coded_font_substitute("C0N200B0")[0].startswith("Times")
    assert _coded_font_substitute("C0H200B0")[0].startswith("Arial")
    assert _coded_font_substitute("ZZZ") is None


def test_rule_thickness_floor_tracks_resolution() -> None:
    # FOP underlines are 1-2 L-units at 240/inch and must stay thin, not be
    # clamped up to the old fixed 10 (which was ~6x too thick there).
    from readafp.parser import iter_fields
    from readafp import ptoca
    afp = TESTDATA / "fop-pairs" / "textdeko.afp"
    if not afp.exists():
        pytest.skip("fop-pairs not present")
    page = ptoca.extract_pages(list(iter_fields(afp.read_bytes())))[0]
    underlines = [abs(r.thickness) for r in page.rules if abs(r.thickness) < 50]
    assert underlines and max(underlines) <= 2  # thin, not clamped to 10


def test_fop_inline_runs_use_monospace() -> None:
    from readafp.parser import iter_fields
    from readafp import ptoca
    afp = TESTDATA / "fop-pairs" / "textdeko.afp"
    if not afp.exists():
        pytest.skip("fop-pairs not present")
    page = ptoca.extract_pages(list(iter_fields(afp.read_bytes())))[0]
    inline = [r for r in page.texts if "fo:inline" in r.text]
    assert inline and all("monospace" in r.font_family for r in inline)
