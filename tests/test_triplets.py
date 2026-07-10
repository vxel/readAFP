"""Tests for MO:DCA triplet decoding and MCF code-page detection."""

from pathlib import Path

import pytest

from readafp.parser import StructuredField, iter_fields, parse_file
from readafp.ptoca import extract_pages
from readafp.triplets import (
    codec_for_codepage_name,
    codec_for_cpgid,
    describe_field,
    describe_triplet,
    field_triplets,
    mcf_coded_fonts,
    mcf_font_resources,
    parse_mcf_codepages,
)

TESTDATA = Path(__file__).parent.parent / "testdata"
HEALTH_SAMPLE = TESTDATA / "sample1_health" / "01_Health_Coverage.afp"
FOP_SIMPLE = TESTDATA / "fop-pairs" / "simple.afp"
MCF1_SAMPLE = TESTDATA / "Sample Files" / "Sample 1.afp"


def _field(sf_id: int, data: bytes) -> StructuredField:
    return StructuredField(offset=0, sf_id=sf_id, flags=0, sequence=0, data=data)


# One MCF format-2 repeating group: FQN code page "T1000273" + coded
# font local id 1.
_CP_FQN = bytes([12, 0x02, 0x85, 0x00]) + "T1000273".encode("cp500")
_LOCAL_ID = bytes([4, 0x24, 0x05, 0x01])
_MCF2_GROUP = (
    (2 + len(_CP_FQN) + len(_LOCAL_ID)).to_bytes(2, "big") + _CP_FQN + _LOCAL_ID
)


def test_codec_for_cpgid() -> None:
    assert codec_for_cpgid(500) == "cp500"
    assert codec_for_cpgid(37) == "cp037"  # needs zero padding
    assert codec_for_cpgid(1208) == "utf-8"
    assert codec_for_cpgid(0) is None
    assert codec_for_cpgid(99999) is None


def test_codec_for_codepage_name() -> None:
    assert codec_for_codepage_name("T1V10500") == "cp500"
    assert codec_for_codepage_name("T1001140") == "cp1140"
    assert codec_for_codepage_name("T1AAAAAA") is None  # custom code page
    assert codec_for_codepage_name("T1EX0000") is None  # CPGID 0


def test_begin_field_triplets_after_name() -> None:
    # 0x65 Comment rides directly after the 8-byte token name.
    field = _field(0xD3A85F, b"\x40" * 8 + bytes([7, 0x65]) + b"hello")
    assert field_triplets(field) == [(None, 0x65, b"hello")]


def test_bdt_triplets_after_reserved_bytes() -> None:
    # BDT: name(8) + reserved(2) + CGCSGID, as FOP writes it.
    data = b"\x40" * 8 + b"\x00\x06" + bytes.fromhex("0601ffff01f4")
    triplets = field_triplets(_field(0xD3A8A8, data))
    assert triplets == [(None, 0x01, bytes.fromhex("ffff01f4"))]


def test_non_triplet_tail_yields_nothing() -> None:
    # A Begin field whose tail is not a clean run of triplets shows hex only.
    assert field_triplets(_field(0xD3A8A8, b"\x40" * 8 + b"\x00\x00")) == []
    assert field_triplets(_field(0xD3A8A8, b"\x40" * 8 + b"\xff\x65zz")) == []


def test_data_fields_have_no_triplet_view() -> None:
    assert field_triplets(_field(0xD3EE9B, bytes([4, 0x65, 1, 2]))) == []


def test_grouped_triplets_tag_group_index() -> None:
    field = _field(0xD3AB8A, _MCF2_GROUP * 2)
    triplets = field_triplets(field)
    assert [g for g, _tid, _td in triplets] == [0, 0, 1, 1]
    assert {tid for _g, tid, _td in triplets} == {0x02, 0x24}


def test_grouped_triplets_reject_short_group() -> None:
    assert field_triplets(_field(0xD3AB8A, _MCF2_GROUP + b"\x00")) == []


def test_describe_triplet_decodes_common_ids() -> None:
    assert describe_triplet(0x01, bytes.fromhex("000001f4")) == "CCSID 500 → cp500"
    assert (
        describe_triplet(0x02, b"\x85\x00" + "T1V10500".encode("cp500"))
        == "Code Page Name Reference: T1V10500 → cp500"
    )
    assert describe_triplet(0x24, b"\x05\x02") == "coded font, local id 2"
    assert describe_triplet(0x26, b"\x2d\x00") == "90°"
    assert describe_triplet(0x65, b"plain ascii") == '"plain ascii" (ASCII)'
    assert describe_triplet(0x65, "ebcdic".encode("cp500")) == '"ebcdic" (EBCDIC)'
    assert describe_triplet(0x8B, b"\x00\x00\x00\xb4") == "9 pt"
    assert describe_triplet(0x4C, b"\x02\x00\x03\x20\x00\x01\x40") == "800 × 320"


def test_describe_triplet_utf16_fqn_name() -> None:
    name = "Arial Regular".encode("utf-16-be")
    detail = describe_triplet(0x02, b"\x01\x00" + name)
    assert "Arial Regular" in detail


def test_parse_mcf2_codepages() -> None:
    mapping = parse_mcf_codepages(_MCF2_GROUP)
    assert mapping[1].name == "T1000273"
    assert mapping[1].codec == "cp273"


def test_parse_mcf2_prefers_cgcsgid_codec() -> None:
    # A custom code page name resolves to nothing, but the CGCSGID's
    # CCSID 500 still pins the codec.
    cgcsgid = bytes([6, 0x01]) + bytes.fromhex("000001f4")
    fqn = bytes([12, 0x02, 0x85, 0x00]) + "T1AAAAAA".encode("cp500")
    body = cgcsgid + fqn + _LOCAL_ID
    group = (2 + len(body)).to_bytes(2, "big") + body
    mapping = parse_mcf_codepages(group)
    assert mapping[1].name == "T1AAAAAA"
    assert mapping[1].codec == "cp500"


def test_fop_mcf_labels_cp500() -> None:
    if not FOP_SIMPLE.exists():
        pytest.skip("FOP pairs not present")
    fields = parse_file(str(FOP_SIMPLE))
    mcf = next(f for f in fields if f.sf_id == 0xD3AB8A)
    mapping = parse_mcf_codepages(mcf.data)
    assert mapping and all(cp.name == "T1V10500" for cp in mapping.values())
    assert all(cp.codec == "cp500" for cp in mapping.values())
    rows = describe_field(mcf)
    expected = "Code Page Name Reference: T1V10500 → cp500"
    assert any(r["detail"] == expected for r in rows)


def test_mcf1_sample_labels_cp1140() -> None:
    if not MCF1_SAMPLE.exists():
        pytest.skip("test corpus not present")
    fields = parse_file(str(MCF1_SAMPLE))
    mcf = next(f for f in fields if f.sf_id == 0xD3B18A)
    mapping = parse_mcf_codepages(mcf.data, format1=True)
    assert mapping[1].name == "T1001140" and mapping[1].codec == "cp1140"
    rows = describe_field(mcf)
    assert "code page T1001140 → cp1140" in rows[0]["detail"]
    assert "character set C0AAAAN1" in rows[0]["detail"]


def test_mcf2_coded_font_fqn() -> None:
    # A format-2 MCF that names a coded font (FQN X'8E') beside the X'24'
    # Resource Local Id — the classic mapping readAFP now follows.
    def group(lid: int, cf: str) -> bytes:
        name = cf.encode("cp500")
        fqn = bytes([2 + 2 + len(name), 0x02, 0x8E, 0x00]) + name
        rlid = bytes([0x04, 0x24, 0x05, lid])
        body = fqn + rlid
        return (len(body) + 2).to_bytes(2, "big") + body

    data = group(1, "X0TRTWSP") + group(2, "X0TRTABC")
    assert mcf_coded_fonts(data, format1=False) == {1: "X0TRTWSP", 2: "X0TRTABC"}
    # No char-set / code-page FQN present, so those come back empty.
    assert mcf_font_resources(data, format1=False) == {1: (None, None),
                                                        2: (None, None)}


def test_mcf1_ff_prefixed_name_is_absent() -> None:
    # A format-1 group naming only a coded font, with the code-page and
    # char-set slots filled X'FFFF0000…' (the "not present" sentinel):
    # those slots must decode to None, not garbage, so the coded-font name
    # is used instead.
    rg_len = 30
    header = bytes([rg_len, 0, 0, 0])
    cf = "X1ARBF  ".encode("cp500")            # 8-byte coded font name
    absent = b"\xff\xff" + b"\x00" * 6         # partial-FF sentinel
    group = bytes([1, 0, 0, 0]) + cf + absent + absent + b"\x00\x00"
    assert len(group) == rg_len
    data = header + group
    assert mcf_coded_fonts(data, format1=True) == {1: "X1ARBF"}
    assert mcf_font_resources(data, format1=True) == {1: (None, None)}


def test_describe_field_health_mdr() -> None:
    if not HEALTH_SAMPLE.exists():
        pytest.skip("test corpus not present")
    fields = parse_file(str(HEALTH_SAMPLE))
    mdr = next(f for f in fields if f.sf_id == 0xD3ABC3)
    rows = describe_field(mdr)
    details = " | ".join(r["detail"] for r in rows)
    assert "Arial Bold" in details  # the font FQN, decoded
    assert "9 pt" in details  # its data-object font descriptor


def _sf(sf_id: int, data: bytes = b"") -> bytes:
    body = sf_id.to_bytes(3, "big") + b"\x00\x00\x00" + data
    return b"\x5a" + (len(body) + 2).to_bytes(2, "big") + body


def test_extract_pages_uses_mcf_codepage_per_font() -> None:
    text = "Hällö".encode("cp273")
    # SCFL selects font 1 (labeled cp273 by the MCF), then TRN.
    ptx = bytes.fromhex("2bd3" "03f101") + bytes([2 + len(text), 0xDA]) + text
    doc = (
        _sf(0xD3A8A8, b"\x40" * 8)  # BDT
        + _sf(0xD3AB8A, _MCF2_GROUP)  # MCF maps font 1 -> T1000273
        + _sf(0xD3A8AF, b"\x40" * 8)  # BPG
        + _sf(0xD3EE9B, ptx)
        + _sf(0xD3A9AF, b"\x40" * 8)  # EPG
        + _sf(0xD3A9A8, b"\x40" * 8)  # EDT
    )
    pages = extract_pages(list(iter_fields(doc)))  # default stays cp500
    assert pages[0].texts[0].text == "Hällö"


def test_unlabeled_font_falls_back_to_chosen_codepage() -> None:
    text = "Hällö".encode("cp273")
    ptx = bytes.fromhex("2bd3") + bytes([2 + len(text), 0xDA]) + text
    doc = (
        _sf(0xD3A8A8, b"\x40" * 8)
        + _sf(0xD3A8AF, b"\x40" * 8)
        + _sf(0xD3EE9B, ptx)
        + _sf(0xD3A9AF, b"\x40" * 8)
        + _sf(0xD3A9A8, b"\x40" * 8)
    )
    fields = list(iter_fields(doc))
    assert extract_pages(fields, codepage="cp273")[0].texts[0].text == "Hällö"
    assert extract_pages(fields, codepage="cp500")[0].texts[0].text != "Hällö"
