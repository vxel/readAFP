"""Tests for BCOCA bar code decoding and page placement."""

from pathlib import Path

import pytest

from readafp.bcoca import (
    TYPE_DATA_MATRIX,
    TYPE_QR,
    barcode_png,
    parse_barcode,
)
from readafp.parser import parse_file
from readafp.ptoca import extract_pages
from readafp.render import page_to_svg

TESTDATA = Path(__file__).parent.parent / "testdata"
DM_SAMPLE = TESTDATA / "alpheus-corpus" / "external" / "afplib_ende.afp"
QR_SAMPLE = TESTDATA / "bcoca_sample.afp"

# The BDD/BDA bytes of the afplib bar code (the corpus's only BCOCA):
# a Data Matrix symbol (BSD type X'1C'), historically misread here as QR.
BDD = bytes.fromhex("00000bb80bb8ffffffff00001c00ff000010ffff01ffff")
BDA = bytes.fromhex("8000010001000012001200000000 00".replace(" ", "")) + (
    b"0010010100641000055100000000000000"
)

# The same shape re-typed as a genuine QR Code (X'20' modifier X'02'). QR
# special-function parameters end at byte 13, so the data starts at 14 —
# one byte earlier than Data Matrix.
QR_BDD = BDD[:12] + bytes([TYPE_QR, 0x02]) + BDD[14:]
QR_BDA = BDA[:14] + BDA[15:]


def test_parse_barcode_fixture_is_data_matrix() -> None:
    bar = parse_barcode(BDD, BDA)
    assert bar.bc_type == TYPE_DATA_MATRIX
    assert bar.upi == 300
    assert bar.module_mils == 16
    assert (bar.x, bar.y) == (1, 1)
    assert bar.data == "0010010100641000055100000000000000"


def test_data_matrix_is_skipped_not_drawn_as_qr() -> None:
    # Drawing a Data Matrix as a QR look-alike would fabricate a wrong
    # (unscannable) symbol; the renderer must skip it instead.
    assert barcode_png(parse_barcode(BDD, BDA)) is None


def test_parse_qr_fields() -> None:
    bar = parse_barcode(QR_BDD, QR_BDA)
    assert bar.bc_type == TYPE_QR
    assert bar.version == 18
    assert bar.ec_level == 0  # level L
    assert bar.data == "0010010100641000055100000000000000"


def test_barcode_png_generates_requested_version() -> None:
    png, modules = barcode_png(parse_barcode(QR_BDD, QR_BDA))
    assert modules == 89  # QR version 18 is 89x89 modules
    assert png.startswith(b"\x89PNG")


def test_barcode_png_grows_when_version_too_small() -> None:
    bar = parse_barcode(QR_BDD, QR_BDA)
    bar.version = 1  # 21x21 cannot hold 500 digits
    bar.data = "9" * 500
    png, modules = barcode_png(bar)
    assert modules > 21


def test_barcode_png_data_too_large_returns_none() -> None:
    bar = parse_barcode(QR_BDD, QR_BDA)
    bar.data = "9" * 8000  # exceeds every QR version; must not raise
    assert barcode_png(bar) is None


def test_barcode_png_rejects_unknown_symbology() -> None:
    bar = parse_barcode(QR_BDD, QR_BDA)
    bar.bc_type = 0x01  # Code 39: not generated, must not draw wrong
    assert barcode_png(bar) is None


def test_parse_barcode_suppressed_symbol() -> None:
    suppressed = bytes([BDA[0] | 0x04]) + BDA[1:]
    assert parse_barcode(BDD, suppressed) is None


def test_metric_unit_base_converted() -> None:
    bdd = bytearray(BDD)
    bdd[0] = 0x01  # unit base X'01': units per 10 centimeters
    bdd[2:4] = (3937).to_bytes(2, "big")  # 393.7/cm = 1000/inch
    assert parse_barcode(bytes(bdd), BDA).upi == 1000


def test_corpus_data_matrix_not_placed_on_page() -> None:
    if not DM_SAMPLE.exists():
        pytest.skip("test corpus not present")
    pages = extract_pages(parse_file(str(DM_SAMPLE)))
    assert not [img for page in pages for img in page.images]


def test_qr_sample_placed_and_pixelated() -> None:
    if not QR_SAMPLE.exists():
        pytest.skip("run tools/make_bcoca_sample.py to generate the sample")
    pages = extract_pages(parse_file(str(QR_SAMPLE)))
    images = [img for page in pages for img in page.images]
    assert len(images) == 1 and images[0].crisp
    svg = page_to_svg(next(p for p in pages if p.images))
    assert "image-rendering:pixelated" in svg
    assert 'href="data:image/png;base64,' in svg
