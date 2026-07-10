"""MO:DCA triplet decoding and per-field triplet location.

A triplet is a self-identifying parameter:

    u8 length            counts the length and id bytes too
    u8 id                triplet identifier
    data                 (length - 2 bytes)

Triplets ride on most structured fields, either directly after a fixed
prefix (Begin/End fields put them after the 8-byte name) or inside
repeating groups introduced by a u16 group length (MCF, MDR, MPS, ...).

Locating them is per-field-type knowledge, kept in the registries below.
A run of triplets is only reported when it parses cleanly through to the
end of its slot; anything else is shown as plain hex by the caller, so a
wrong offset can't dress arbitrary bytes up as decoded structure.

Reference: MO:DCA Reference, AFPC-0004-10 (docs/specs/modca-reference-10.pdf).
"""

import codecs
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from readafp.parser import StructuredField

logger = logging.getLogger(__name__)

TRIPLET_NAMES = {
    0x01: "Coded Graphic Character Set Global Identifier",
    0x02: "Fully Qualified Name",
    0x04: "Mapping Option",
    0x10: "Object Classification",
    0x18: "MO:DCA Interchange Set",
    0x1F: "Font Descriptor Specification",
    0x21: "Object Function Set Specification",
    0x22: "Extended Resource Local Identifier",
    0x24: "Resource Local Identifier",
    0x25: "Resource Section Number",
    0x26: "Character Rotation",
    0x2D: "Object Byte Offset",
    0x36: "Attribute Value",
    0x43: "Descriptor Position",
    0x45: "Media Eject Control",
    0x46: "Page Overlay Conditional Processing",
    0x47: "Resource Usage Attribute",
    0x4B: "Measurement Units",
    0x4C: "Object Area Size",
    0x4D: "Area Definition",
    0x4E: "Color Specification",
    0x50: "Encoding Scheme ID",
    0x56: "Medium Map Page Number",
    0x57: "Object Byte Extent",
    0x58: "Object Structured Field Offset",
    0x59: "Object Structured Field Extent",
    0x5A: "Object Offset",
    0x62: "Local Date and Time Stamp",
    0x65: "Comment",
    0x68: "Medium Orientation",
    0x6C: "Resource Object Include",
    0x70: "Presentation Space Reset Mixing",
    0x71: "Presentation Space Mixing Rule",
    0x72: "Universal Date and Time Stamp",
    0x74: "Toner Saver",
    0x75: "Color Fidelity",
    0x78: "Font Fidelity",
    0x80: "Attribute Qualifier",
    0x81: "Page Position Information",
    0x82: "Parameter Value",
    0x83: "Presentation Control",
    0x84: "Font Resolution and Metric Technology",
    0x85: "Finishing Operation",
    0x86: "Text Fidelity",
    0x87: "Media Fidelity",
    0x88: "Finishing Fidelity",
    0x8B: "Data Object Font Descriptor",
    0x8C: "Locale Selector",
    0x8E: "UP3i Finishing Operation",
    0x91: "Color Management Resource Descriptor",
    0x95: "Rendering Intent",
    0x96: "CMR Tag Fidelity",
    0x97: "Device Appearance",
    0x9A: "Image Resolution",
    0x9C: "Object Container Presentation Space Size",
}

# Fully Qualified Name (0x02) type codes.
FQN_TYPES = {
    0x01: "Replace First GID Name",
    0x07: "Font Family Name",
    0x08: "Font Typeface Name",
    0x09: "MO:DCA Resource Hierarchy Reference",
    0x0A: "Begin Resource Group Reference",
    0x0B: "Attribute GID",
    0x0C: "Process Element GID",
    0x0D: "Begin Page Group Reference",
    0x11: "Media Type Reference",
    0x41: "Color Management Resource Reference",
    0x6E: "Data Object Font Base Font Identifier",
    0x7E: "Data Object Font Linked Font Identifier",
    0x83: "Begin Document Reference",
    0x84: "Resource Object Reference",
    0x85: "Code Page Name Reference",
    0x86: "Font Character Set Name Reference",
    0x87: "Begin Document Index Reference",
    0x8D: "Begin Overlay Reference",
    0x8E: "Data Object Resource Reference",
    0xBE: "Coded Font Name Reference",
    0xCE: "Other Object Data Reference",
    0xDE: "Data Object External Resource Reference",
}

_OBJECT_CLASSES = {
    0x01: "time-invariant paginated presentation object",
    0x10: "time-variant presentation object",
    0x20: "executable program",
    0x30: "setup file",
    0x40: "secondary resource",
    0x41: "data-object font",
}

_RESOURCE_ID_TYPES = {
    0x00: "usage-dependent",
    0x02: "page overlay",
    0x05: "coded font",
    0x07: "color attribute table",
}

_ROTATIONS = {0x0000: "0°", 0x2D00: "90°", 0x5A00: "180°", 0x8700: "270°"}

_ORIENTATIONS = {
    0x00: "portrait",
    0x01: "landscape",
    0x02: "reverse portrait",
    0x03: "reverse landscape",
    0x04: "portrait 90°",
    0x05: "landscape 90°",
}

# Structured fields whose data is a fixed prefix followed by triplets;
# values are candidate start offsets, tried in order (the MO:DCA editions
# disagree on some reserved-byte counts, and the clean-parse guard picks
# the offset that actually fits the bytes).
_TRIPLET_OFFSETS: Dict[int, Tuple[int, ...]] = {
    0xD3A090: (0,),  # TLE: triplets only
    0xD3A66B: (0,),  # OBD: triplets only
    0xD3ABCC: (8,),  # IMM: name(8)
    0xD3AFC3: (27,),  # IOB: object placement prefix (see ptoca._parse_iob)
    0xD3AF5F: (14,),  # IPS: name(8) + Xpsoset(3) + Ypsoset(3)
    0xD3AFD8: (14,),  # IPO: name(8) + Xoloset(3) + Yoloset(3)
    0xD3A6AF: (15, 14),  # PGD: bases/units/size + reserved
    0xD3A688: (13,),  # MDD: bases/units/size + flags byte
    0xD3B19B: (14, 12),  # PTD format 2
}

# Structured fields built of u16-length repeating groups of triplets.
_GROUPED_SFS = {
    0xD3AB8A,  # MCF format 2
    0xD3ABC3,  # MDR
    0xD3ABAF,  # MPS
    0xD3ABD8,  # MPO
    0xD3ABEB,  # MBC
}

MCF_FORMAT_1 = 0xD3B18A
MCF_FORMAT_2 = 0xD3AB8A


def _u16(b: bytes, off: int = 0) -> int:
    return int.from_bytes(b[off : off + 2], "big")


def iter_triplets(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """Yield (triplet id, triplet data) from a run of MO:DCA triplets."""
    pos = 0
    while pos + 2 <= len(data):
        length, tid = data[pos], data[pos + 1]
        if length < 2 or pos + length > len(data):
            break
        yield tid, bytes(data[pos + 2 : pos + length])
        pos += length


def _consume_triplets(data: bytes) -> Optional[List[Tuple[int, bytes]]]:
    """Parse triplets that must tile ``data`` exactly, else return None."""
    triplets: List[Tuple[int, bytes]] = []
    pos = 0
    while pos + 2 <= len(data):
        length = data[pos]
        if length < 2 or pos + length > len(data):
            return None
        triplets.append((data[pos + 1], bytes(data[pos + 2 : pos + length])))
        pos += length
    return triplets if triplets and pos == len(data) else None


def field_triplets(
    field: StructuredField,
) -> List[Tuple[Optional[int], int, bytes]]:
    """Locate a field's triplets as (group index or None, id, data).

    Returns an empty list when the field type carries no triplets or its
    bytes do not parse cleanly as a run of them.
    """
    data = field.data
    if field.sf_id in _GROUPED_SFS:
        return _grouped_triplets(data)
    offsets = _TRIPLET_OFFSETS.get(field.sf_id)
    if offsets is None and field.type_code in (0xA8, 0xA9):
        # Begin/End: 8-byte name, sometimes followed by 2 reserved bytes
        # (BDT, BRS), or a 2-byte X'FFFF' "match any" token instead.
        offsets = (8, 10, 2) if data[:2] == b"\xff\xff" else (8, 10)
    if offsets is None:
        return []
    for off in offsets:
        if len(data) > off:
            triplets = _consume_triplets(data[off:])
            if triplets:
                return [(None, tid, tdata) for tid, tdata in triplets]
    return []


def _grouped_triplets(data: bytes) -> List[Tuple[Optional[int], int, bytes]]:
    """Triplets from u16-length repeating groups that must tile the field."""
    out: List[Tuple[Optional[int], int, bytes]] = []
    pos = 0
    group = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            return []
        triplets = _consume_triplets(data[pos + 2 : pos + group_len])
        if triplets is None:
            return []
        out.extend((group, tid, tdata) for tid, tdata in triplets)
        pos += group_len
        group += 1
    return out if out and pos == len(data) else []


# Non-numeric or zero-valued names (custom code pages) resolve to None.
_CODEC_SPECIALS = {
    367: "ascii",
    819: "latin-1",
    1200: "utf-16-be",
    1208: "utf-8",
    13488: "utf-16-be",
}


def codec_for_cpgid(cpgid: int) -> Optional[str]:
    """Map an IBM code page / CCSID number to a Python codec, if one exists."""
    if cpgid <= 0:
        return None
    for cand in (_CODEC_SPECIALS.get(cpgid), f"cp{cpgid}", f"cp{cpgid:03d}"):
        if not cand:
            continue
        try:
            codecs.lookup(cand)
            return cand
        except LookupError:
            pass
    return None


def codec_for_codepage_name(name: str) -> Optional[str]:
    """Resolve a code page name like 'T1V10500' or 'T1001140' to a codec.

    IBM code page names end in the CPGID digits; the leading characters
    are registry/version prefixes ('T1', 'V1', ...).
    """
    for digits in (5, 4):
        tail = name[-digits:]
        if len(tail) == digits and tail.isdigit():
            codec = codec_for_cpgid(int(tail))
            if codec:
                return codec
    return None


def _ebcdic(b: bytes) -> str:
    return b.decode("cp500", errors="replace").strip()


def _name_string(b: bytes) -> str:
    """Decode a GID character string: UTF-16BE when it looks like it.

    Producers that declare CCSID 1200 (e.g. for TrueType resources) encode
    FQN names in UTF-16BE; Latin text there has a zero high byte for
    nearly every character. Everything else is EBCDIC.
    """
    if len(b) >= 2 and len(b) % 2 == 0:
        if sum(1 for hi in b[0::2] if hi == 0) >= len(b) // 2 * 0.8:
            try:
                return b.decode("utf-16-be").strip()
            except UnicodeDecodeError:
                pass
    return _ebcdic(b)


def _cgcsgid_detail(tdata: bytes) -> str:
    if len(tdata) < 4:
        return ""
    gcsgid, cpgid = _u16(tdata), _u16(tdata, 2)
    # A zero GCSGID means the second half is a CCSID, not a CPGID.
    label = f"CCSID {cpgid}" if gcsgid == 0 else f"GCSGID {gcsgid}, CPGID {cpgid}"
    codec = codec_for_cpgid(cpgid)
    return label + (f" → {codec}" if codec else "")


def _fqn_detail(tdata: bytes) -> str:
    if len(tdata) < 2:
        return ""
    fqn_type, fqn_format = tdata[0], tdata[1]
    raw = tdata[2:]
    if fqn_format == 0x20:  # URL
        name = raw.decode("ascii", errors="replace")
    elif fqn_format == 0x10:  # ASN.1 OID
        name = "OID " + raw.hex()
    else:  # character string
        name = _name_string(raw)
    detail = f"{FQN_TYPES.get(fqn_type, f'type 0x{fqn_type:02X}')}: {name}"
    if fqn_type == 0x85:
        codec = codec_for_codepage_name(name)
        if codec:
            detail += f" → {codec}"
    return detail


def _objclass_detail(tdata: bytes) -> str:
    if not tdata:
        return ""
    parts = [_OBJECT_CLASSES.get(tdata[0], f"class 0x{tdata[0]:02X}")]
    if len(tdata) >= 53:
        type_name = _ebcdic(tdata[21:53])
        if type_name:
            parts.append(type_name)
    return ", ".join(parts)


def _resource_id_detail(tdata: bytes) -> str:
    if len(tdata) < 2:
        return ""
    kind = _RESOURCE_ID_TYPES.get(tdata[0], f"type 0x{tdata[0]:02X}")
    return f"{kind}, local id {tdata[1]}"


def _rotation_detail(tdata: bytes) -> str:
    if len(tdata) < 2:
        return ""
    value = _u16(tdata)
    return _ROTATIONS.get(value, f"X'{value:04X}'")


def _units_detail(tdata: bytes) -> str:
    if len(tdata) < 6:
        return ""
    base = {0x00: "10 in", 0x01: "10 cm"}.get(tdata[0], f"base 0x{tdata[0]:02X}")
    return f"{_u16(tdata, 2)} × {_u16(tdata, 4)} units per {base}"


def _area_size_detail(tdata: bytes) -> str:
    if len(tdata) < 7:
        return ""
    x = int.from_bytes(tdata[1:4], "big")
    y = int.from_bytes(tdata[4:7], "big")
    return f"{x} × {y}"


def _datetime_detail(tdata: bytes) -> str:
    if len(tdata) < 15:
        return ""
    kind = {0x00: "created", 0x01: "retired", 0x03: "revised"}.get(
        tdata[0], f"type 0x{tdata[0]:02X}"
    )
    # EBCDIC digits: century flag, YY, day-of-year DDD, HH, MM, SS, hundredths.
    s = tdata[1:15].decode("cp500", errors="replace")
    century, yy, ddd = s[0], s[1:3], s[3:6]
    hh, mm, ss = s[6:8], s[8:10], s[10:12]
    if not yy.isdigit():
        return kind
    base = 1900 if century in (" ", "0") else None
    if base is None and century.isdigit():
        base = 1900 + 100 * int(century)
    if base is None:
        return kind
    return f"{kind} {base + int(yy)} day {ddd}, {hh}:{mm}:{ss}"


def _comment_detail(tdata: bytes) -> str:
    if not tdata:
        return ""
    if all(0x20 <= b < 0x7F or b == 0x09 for b in tdata):
        return f'"{tdata.decode("ascii")}" (ASCII)'
    return f'"{_ebcdic(tdata)}" (EBCDIC)'


def _attribute_value_detail(tdata: bytes) -> str:
    if len(tdata) < 2:
        return ""
    return f'"{_ebcdic(tdata[2:])}"'


def _orientation_detail(tdata: bytes) -> str:
    if not tdata:
        return ""
    return _ORIENTATIONS.get(tdata[0], f"0x{tdata[0]:02X}")


def _font_descriptor_detail(tdata: bytes) -> str:
    if len(tdata) < 4:
        return ""
    return f"{_u16(tdata, 2) / 20:g} pt"


def _byte_extent_detail(tdata: bytes) -> str:
    if len(tdata) < 4:
        return ""
    return f"{int.from_bytes(tdata[:4], 'big')} bytes"


def _encoding_scheme_detail(tdata: bytes) -> str:
    if len(tdata) < 2:
        return ""
    return f"scheme X'{_u16(tdata):04X}'"


_DESCRIBERS = {
    0x01: _cgcsgid_detail,
    0x02: _fqn_detail,
    0x10: _objclass_detail,
    0x24: _resource_id_detail,
    0x25: lambda t: f"section {t[0]}" if t else "",
    0x26: _rotation_detail,
    0x36: _attribute_value_detail,
    0x4B: _units_detail,
    0x4C: _area_size_detail,
    0x50: _encoding_scheme_detail,
    0x57: _byte_extent_detail,
    0x62: _datetime_detail,
    0x65: _comment_detail,
    0x68: _orientation_detail,
    0x8B: _font_descriptor_detail,
}


def describe_triplet(tid: int, tdata: bytes) -> str:
    """Human-readable decode of a triplet's data; '' when none is known."""
    describer = _DESCRIBERS.get(tid)
    return describer(tdata) if describer else ""


def describe_field(field: StructuredField) -> List[Dict[str, Any]]:
    """Decode a field's triplets into display rows for the inspector.

    Each row carries the triplet id, name, decoded detail (may be empty),
    raw hex, and the repeating-group index for grouped fields. MCF format
    1 is not triplet-based; its fixed groups get one row each.
    """
    if field.sf_id == MCF_FORMAT_1:
        return _describe_mcf1(field.data)
    rows: List[Dict[str, Any]] = []
    for group, tid, tdata in field_triplets(field):
        rows.append(
            {
                "id": f"0x{tid:02X}",
                "name": TRIPLET_NAMES.get(tid, "Unknown triplet"),
                "detail": describe_triplet(tid, tdata),
                "hex": tdata.hex(" "),
                "group": group,
            }
        )
    return rows


def _mcf1_groups(
    data: bytes,
) -> List[Tuple[int, Optional[str], Optional[str], Optional[str], bytes]]:
    """MCF format-1 fixed repeating groups.

    Header: RG length(1) + reserved(3). Each group: coded font local
    id(1), reserved(3), coded font name(8), code page name(8), font
    character set name(8); X'FF...' means "not present".
    """
    if len(data) < 4:
        return []
    rg_len = data[0]
    if rg_len < 28 or (len(data) - 4) % rg_len:
        return []

    def name(rg: bytes, start: int) -> Optional[str]:
        raw = rg[start : start + 8]
        # X'FF' fill is the "not present" sentinel; a name never begins with
        # it. Some producers write only a partial fill (X'FFFF0000…') for an
        # absent slot, so treat any leading X'FF' as absent, not garbage.
        if not raw or raw[0] == 0xFF:
            return None
        try:
            return raw.decode("cp500").strip("\x00 ").strip() or None
        except UnicodeDecodeError:
            return None

    out = []
    for pos in range(4, len(data), rg_len):
        rg = data[pos : pos + rg_len]
        out.append((rg[0], name(rg, 4), name(rg, 12), name(rg, 20), rg))
    return out


def _describe_mcf1(data: bytes) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group, (local_id, cf_name, cp_name, fcs_name, raw) in enumerate(
        _mcf1_groups(data)
    ):
        bits = [f"coded font local id {local_id}"]
        if cf_name:
            bits.append(f"coded font {cf_name}")
        if cp_name:
            codec = codec_for_codepage_name(cp_name)
            bits.append(f"code page {cp_name}" + (f" → {codec}" if codec else ""))
        if fcs_name:
            bits.append(f"character set {fcs_name}")
        rows.append(
            {
                "id": "—",
                "name": "Coded-font mapping",
                "detail": " · ".join(bits),
                "hex": raw.hex(" "),
                "group": group,
            }
        )
    return rows


@dataclass
class McfCodePage:
    """A code page an MCF assigned to a coded-font local id."""

    name: str  # as labeled in the file ("T1V10500", "CCSID 500", ...)
    codec: Optional[str]  # Python codec, when the label resolves to one


def parse_mcf_codepages(
    data: bytes, format1: bool = False
) -> Dict[int, McfCodePage]:
    """Extract code page assignments per coded-font local id from an MCF.

    Format 2 carries them as FQN type 0x85 (code page name) and/or 0x01
    CGCSGID triplets next to a 0x24 Resource Local ID; format 1 puts the
    code page name at a fixed slot in each repeating group.
    """
    if format1:
        return {
            local_id: McfCodePage(cp_name, codec_for_codepage_name(cp_name))
            for local_id, _cf, cp_name, _fcs, _raw in _mcf1_groups(data)
            if cp_name
        }
    out: Dict[int, McfCodePage] = {}
    pos = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            break
        local_id: Optional[int] = None
        cp_name: Optional[str] = None
        cgcsgid: Optional[Tuple[int, int]] = None
        for tid, tdata in iter_triplets(data[pos + 2 : pos + group_len]):
            if tid == 0x24 and len(tdata) >= 2 and tdata[0] in (0x00, 0x05):
                local_id = tdata[1]
            elif tid == 0x02 and len(tdata) >= 3 and tdata[0] == 0x85:
                cp_name = _ebcdic(tdata[2:])
            elif tid == 0x01 and len(tdata) >= 4:
                cgcsgid = (_u16(tdata), _u16(tdata, 2))
        codec = None
        if cgcsgid:
            codec = codec_for_cpgid(cgcsgid[1])
        if codec is None and cp_name:
            codec = codec_for_codepage_name(cp_name)
        label = cp_name
        if label is None and cgcsgid:
            kind = "CCSID" if cgcsgid[0] == 0 else "CPGID"
            label = f"{kind} {cgcsgid[1]}"
        if local_id is not None and label:
            out[local_id] = McfCodePage(label, codec)
        pos += group_len
    return out


def mcf_font_resources(
    data: bytes, format1: bool
) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """Map each coded-font local id to its (code page, character set) names.

    Format 1 carries both names in fixed slots per group; format 2 uses
    FQN triplets (X'85' code page, X'86' character set) beside the X'24'
    Resource Local ID. The caller pairs these with embedded code pages and
    character sets to render text in the file's own fonts.
    """
    out: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    if format1:
        for local_id, _cf, cp, fcs, _raw in _mcf1_groups(data):
            out[local_id] = (cp, fcs)
        return out
    pos = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            break
        local_id: Optional[int] = None
        cp_name: Optional[str] = None
        cs_name: Optional[str] = None
        for tid, tdata in iter_triplets(data[pos + 2 : pos + group_len]):
            if tid == 0x24 and len(tdata) >= 2 and tdata[0] in (0x00, 0x05):
                local_id = tdata[1]
            elif tid == 0x02 and len(tdata) >= 3 and tdata[0] == 0x85:
                cp_name = _ebcdic(tdata[2:])
            elif tid == 0x02 and len(tdata) >= 3 and tdata[0] == 0x86:
                cs_name = _ebcdic(tdata[2:])
        if local_id is not None:
            out[local_id] = (cp_name, cs_name)
        pos += group_len
    return out


def mcf_coded_fonts(data: bytes, format1: bool) -> Dict[int, str]:
    """Map each local font id to a coded-font name, when named directly.

    The classic AFP mapping: instead of the char set (FQN X'86') and code
    page (FQN X'85') separately, an MCF group names a single coded font
    (FQN X'8E' in format 2, the fixed coded-font slot in format 1) that
    itself binds the two. The caller resolves that name through the
    embedded coded fonts (see :func:`readafp.foca.parse_coded_fonts`).
    """
    if format1:
        return {
            local_id: cf
            for local_id, cf, _cp, _fcs, _raw in _mcf1_groups(data)
            if cf
        }
    out: Dict[int, str] = {}
    pos = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            break
        local_id: Optional[int] = None
        cf_name: Optional[str] = None
        for tid, tdata in iter_triplets(data[pos + 2 : pos + group_len]):
            if tid == 0x24 and len(tdata) >= 2 and tdata[0] in (0x00, 0x05):
                local_id = tdata[1]
            elif tid == 0x02 and len(tdata) >= 3 and tdata[0] == 0x8E:
                cf_name = _ebcdic(tdata[2:])
        if local_id is not None and cf_name:
            out[local_id] = cf_name
        pos += group_len
    return out


def mcf_resource_names(data: bytes, format1: bool) -> Dict[str, set]:
    """Names of the font resources an MCF references, keyed by kind.

    Returns ``{"coded font": {...}, "code page": {...},
    "character set": {...}}``. Format 1 carries the three names in fixed
    slots per group; format 2 uses FQN triplets (X'85' code page, X'86'
    font character set). The caller diffs these against what the file
    embeds to surface resources it depends on but does not contain.
    """
    refs: Dict[str, set] = {
        "coded font": set(), "code page": set(), "character set": set()
    }
    if format1:
        for _lid, cf, cp, fcs, _raw in _mcf1_groups(data):
            if cf:
                refs["coded font"].add(cf)
            if cp:
                refs["code page"].add(cp)
            if fcs:
                refs["character set"].add(fcs)
        return refs
    pos = 0
    while pos + 2 <= len(data):
        group_len = _u16(data, pos)
        if group_len < 2 or pos + group_len > len(data):
            break
        for tid, tdata in iter_triplets(data[pos + 2 : pos + group_len]):
            if tid == 0x02 and len(tdata) >= 3:
                if tdata[0] == 0x85:
                    refs["code page"].add(_ebcdic(tdata[2:]))
                elif tdata[0] == 0x86:
                    refs["character set"].add(_ebcdic(tdata[2:]))
                elif tdata[0] == 0x8E:
                    refs["coded font"].add(_ebcdic(tdata[2:]))
        pos += group_len
    return refs
