"""Tests for the Flask app's inspect endpoint."""

from pathlib import Path

import pytest

from readafp.app import (
    create_app,
    _field_rows,
    _field_search_text,
    _missing_resources,
    _resource_kind,
)
from readafp.parser import iter_fields
from readafp.parser import parse_file


@pytest.mark.parametrize("decl", ["const svgs", "const dropInput"])
def test_inline_scripts_are_iife_wrapped(decl: str) -> None:
    """The in-browser (Pyodide) path delivers pages via document.write, which
    reuses the JS realm — a top-level const/let then throws "already declared"
    on a 2nd/3rd open and aborts the whole script block (page nav died). Guard
    that the executable inline blocks are wrapped in an IIFE so they declare no
    globals.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    idx = html.index(decl)
    block_start = html.rindex("<script>", 0, idx)
    assert "(function" in html[block_start:idx], (
        f"{decl!r} is at script top level — wrap its block in an IIFE so "
        f"document.write re-opens don't redeclare it."
    )

TESTDATA = Path(__file__).parent.parent / "testdata"
INDEX_HTML = (Path(__file__).parent.parent / "src" / "readafp"
              / "templates" / "index.html")
HEALTH_SAMPLE = TESTDATA / "sample1_health" / "01_Health_Coverage.afp"
OUTLINE_FONT = TESTDATA / "github-samples" / "afplib" / "C0X00006.afp"
CS_OVERLAY = TESTDATA / "github-samples" / "afplib" / "cs.afp"
SAMPLE1 = TESTDATA / "Sample Files" / "Sample 1.afp"
IMAGE_RESOURCE = TESTDATA / "github-samples" / "fop" / "expected_resource.afp"


def test_field_rows_tag_page_membership() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    rows = _field_rows(parse_file(str(HEALTH_SAMPLE)))
    by_name = {row["name"]: row for row in rows}
    # Resource group fields come before any page.
    assert by_name["BRG (Begin Resource Group)"]["page"] is None
    # Page bracket and its contents belong to page 0.
    assert by_name["BPG (Begin Page)"]["page"] == 0
    assert by_name["PTX (Presentation Text Data)"]["page"] == 0
    assert by_name["EPG (End Page)"]["page"] == 0
    # The document close comes after the page.
    assert by_name["EDT (End Document)"]["page"] is None


def test_resource_kind_flags_pageless_font() -> None:
    if not OUTLINE_FONT.exists():
        pytest.skip("outline font fixture not present")
    # A font character set has no BPG, so it is flagged as a resource.
    assert _resource_kind(parse_file(str(OUTLINE_FONT))) == "font character set"


def test_resource_kind_names_specific_object() -> None:
    if not IMAGE_RESOURCE.exists():
        pytest.skip("image resource fixture not present")
    # A resource group wrapping an IOCA image reports the specific object
    # kind, not the generic "resource group".
    assert _resource_kind(parse_file(str(IMAGE_RESOURCE))) == "IOCA image resource"


def test_resource_kind_silent_for_document() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    # A real document has pages, so no resource warning fires.
    assert _resource_kind(parse_file(str(HEALTH_SAMPLE))) == ""


def test_inspect_resource_shows_banner() -> None:
    if not OUTLINE_FONT.exists():
        pytest.skip("outline font fixture not present")
    client = create_app().test_client()
    with OUTLINE_FONT.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, OUTLINE_FONT.name)},
            content_type="multipart/form-data",
        )
    html = response.get_data(as_text=True)
    assert "resource-banner" in html
    assert "font character set" in html


def test_missing_resources_format2_matches_viewer() -> None:
    if not CS_OVERLAY.exists():
        pytest.skip("cs.afp sample not present")
    missing = _missing_resources(parse_file(str(CS_OVERLAY)))
    names = {r["name"] for r in missing}
    # The same external resources a real AFP viewer reports as missing.
    assert "T1EX0000" in names  # code page
    assert {"C0EX0460", "C0EX04U0"} <= names  # character sets
    assert all(r["codec"] is None for r in missing if r["kind"] == "code page")


def test_missing_resources_notes_builtin_codec() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    missing = _missing_resources(parse_file(str(SAMPLE1)))
    # The bulk text's code page is external, but resolves to a built-in
    # codec; the embedded character sets are not reported missing.
    cps = [r for r in missing if r["kind"] == "code page"]
    assert any(r["name"] == "T1001140" and r["codec"] == "cp1140" for r in cps)
    assert not any(r["kind"] == "character set" for r in missing)


def test_no_missing_resources_for_clean_document() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    assert _missing_resources(parse_file(str(HEALTH_SAMPLE))) == []


def test_field_search_text_decodes_nop_and_ptx() -> None:
    # NOP carries human-readable metadata; the Find feature must surface it.
    def sf(sf_id: int, data: bytes) -> bytes:
        body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
        return b"\x5a" + (len(body) + 2).to_bytes(2, "big") + body

    nop = list(iter_fields(sf(0xD3EEEE, "Sample AFP file".encode("cp500"))))[0]
    assert _field_search_text(nop) == "Sample AFP file"
    ptx_data = bytes.fromhex("2bd3") + bytes([2 + 5, 0xDA]) + "Hello".encode("cp500")
    ptx = list(iter_fields(sf(0xD3EE9B, ptx_data)))[0]
    assert "Hello" in _field_search_text(ptx)
    # TLE indexing tag: attribute name (FQN X'02') + value (X'36'). No
    # corpus file carries a TLE, so this validates the layout synthetically.
    def trip(tid: int, data: bytes) -> bytes:
        return bytes([len(data) + 2, tid]) + data

    tle_data = (
        trip(0x02, bytes([0x0B, 0x00]) + "CustomerID".encode("cp500"))
        + trip(0x36, b"\x00\x00" + "12345".encode("cp500"))
    )
    tle = list(iter_fields(sf(0xD3A090, tle_data)))[0]
    assert _field_search_text(tle) == "CustomerID 12345"
    # Other fields carry no searchable text.
    bpg = list(iter_fields(sf(0xD3A8AF, b"\x00" * 8)))[0]
    assert _field_search_text(bpg) == ""


def test_field_data_summaries_decode_geometry() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    rows = {r["name"].split(" ")[0]: r for r in _field_rows(parse_file(str(SAMPLE1)))}
    # Field sizes include the 9-byte SF header (matches AFP inspectors).
    assert rows["FND"]["size"] == 89 and rows["PGD"]["size"] == 24
    # PTD/IDD geometry decode like AFPexplorer.
    assert "12240×15840" in rows["PTD"]["data"] and "1440" in rows["PTD"]["data"]
    assert "778×497" in rows["IDD"]["data"] and "300×300" in rows["IDD"]["data"]


def test_field_data_summaries_decode_font_arrays() -> None:
    if not SAMPLE1.exists():
        pytest.skip("Sample 1 not present")
    all_rows = _field_rows(parse_file(str(SAMPLE1)))
    # FNI uses the enclosing font's FNC record length to list GCGIDs; the
    # TIMES-BOLD font's index starts LA010000, LA020000, LD010000.
    fni = " ".join(r["data"] for r in all_rows if r["name"].startswith("FNI"))
    assert "0=LA010000" in fni and "1=LA020000" in fni
    # FNM lists each pattern's box (+1); that font's first box is 20x21.
    fnm = " ".join(r["data"] for r in all_rows if r["name"].startswith("FNM"))
    assert "0=20x21" in fnm and "1=29x29" in fnm


def test_inspect_endpoint_links_rows_to_pages() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    client = create_app().test_client()
    with HEALTH_SAMPLE.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, HEALTH_SAMPLE.name)},
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-page="0"' in html
    assert 'id="sf-table"' in html
    assert "data:image/jpeg;base64," in html  # logo made it through
    # Plain text export: buttons present and page text embedded.
    assert 'id="copy-text"' in html and 'id="download-text"' in html
    assert "John Doe" in html  # plain_text joins runs in reading order


def test_inspect_endpoint_renders_all_implicit_pages() -> None:
    sample = TESTDATA / "alpheus-corpus" / "large_ibm273.afp"
    if not sample.exists():
        pytest.skip("test corpus not present")
    client = create_app().test_client()
    with sample.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, sample.name)},
            content_type="multipart/form-data",
        )
    html = response.get_data(as_text=True)
    # All 109 flowed pages fit the content budget, so go-to-last works.
    assert 'max="109"' in html
    assert "of 109</span>" in html
    # Loose PTX rows map to the flowed page their text landed on (each
    # PTX spans ~2 pages and starts on an even one), not all to page 1.
    assert 'data-page="2"' in html and 'data-page="50"' in html


def test_inspect_endpoint_codepage_override() -> None:
    sample = TESTDATA / "alpheus-corpus" / "large_ibm273.afp"
    if not sample.exists():
        pytest.skip("test corpus not present")
    client = create_app().test_client()
    with sample.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, sample.name), "codepage": "cp273"},
            content_type="multipart/form-data",
        )
    html = response.get_data(as_text=True)
    assert "Hällö Wörld" in html  # the fixture's German text, decoded
    assert 'value="cp273" selected' in html


def test_inspect_endpoint_shows_triplet_details() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    client = create_app().test_client()
    with HEALTH_SAMPLE.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, HEALTH_SAMPLE.name)},
            content_type="multipart/form-data",
        )
    html = response.get_data(as_text=True)
    # The MDR's decoded triplets are embedded as an expandable detail row.
    assert 'class="trip-detail"' in html
    assert "Fully Qualified Name" in html
    assert "Arial Bold" in html


def test_inspect_endpoint_notes_mcf_codepage() -> None:
    sample = TESTDATA / "fop-pairs" / "simple.afp"
    if not sample.exists():
        pytest.skip("FOP pairs not present")
    client = create_app().test_client()
    with sample.open("rb") as handle:
        response = client.post(
            "/inspect",
            data={"afpfile": (handle, sample.name)},
            content_type="multipart/form-data",
        )
    html = response.get_data(as_text=True)
    assert "code page from MCF: T1V10500 → cp500" in html


def test_inspect_endpoint_rejects_non_afp() -> None:
    client = create_app().test_client()
    response = client.post(
        "/inspect",
        data={"afpfile": (__import__("io").BytesIO(b"not afp"), "x.afp")},
        content_type="multipart/form-data",
    )
    assert b"Not a valid AFP file" in response.data


def test_build_context_is_flask_free_and_complete() -> None:
    # build_context drives both the server and the in-browser (Pyodide) render.
    from readafp.app import build_context
    data = HEALTH_SAMPLE.read_bytes()
    ctx = build_context(data, "health.afp", "cp500")
    for key in ("fields", "page_svgs", "summary", "codepages", "codepage",
                "samples", "mcf_note", "missing_resources", "page_total"):
        assert key in ctx, key
    assert ctx["fields"] and ctx["page_svgs"]
    # bad bytes -> error context, not an exception
    bad = build_context(b"not afp", "x.afp", "cp500")
    assert bad["fields"] is None and bad["error"]


def test_pyodide_zip_route_serves_importable_package() -> None:
    import io
    import zipfile
    c = create_app().test_client()
    r = c.get("/pyodide/readafp.zip")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert "readafp/app.py" in names
    assert "readafp/templates/index.html" in names


def test_index_loads_inbrowser_script() -> None:
    html = create_app().test_client().get("/").get_data(as_text=True)
    assert "inbrowser.js" in html
