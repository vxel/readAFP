"""MO:DCA structured-field parser.

An AFP file is a sequence of structured fields. Each one looks like:

    0x5A                 carriage-control byte (always 0x5A)
    length      (2 bytes, big-endian; counts everything after the 0x5A)
    sf_id       (3 bytes: 0xD3, type code, category code)
    flags       (1 byte)
    sequence    (2 bytes)
    data        (length - 8 bytes; may begin with an 8-byte EBCDIC name
                 for Begin/End fields, and may carry triplets)

Reference: MO:DCA Reference, AFPC-0004-10 (docs/specs/modca-reference-10.pdf).
"""

import logging
from dataclasses import dataclass
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

CARRIAGE_CONTROL = 0x5A

# MO:DCA type codes (second byte of the structured field ID).
TYPE_CODES = {
    0xA0: "Attribute",
    0xA2: "CopyCount",
    0xA6: "Descriptor",
    0xA7: "Control",
    0xA8: "Begin",
    0xA9: "End",
    0xAB: "Map",
    0xAC: "Position",
    0xAD: "Process",
    0xAF: "Include",
    0xB0: "Table",
    0xB1: "Migration",
    0xB2: "Variable",
    0xB4: "Link",
    0xEE: "Data",
}

# Well-known structured fields, keyed by the full 3-byte ID.
# Names follow the MO:DCA reference abbreviations.
SF_NAMES = {
    0xD3A8A8: "BDT (Begin Document)",
    0xD3A9A8: "EDT (End Document)",
    0xD3A8AD: "BNG (Begin Named Page Group)",
    0xD3A9AD: "ENG (End Named Page Group)",
    0xD3A8AF: "BPG (Begin Page)",
    0xD3A9AF: "EPG (End Page)",
    0xD3A8C9: "BAG (Begin Active Environment Group)",
    0xD3A9C9: "EAG (End Active Environment Group)",
    0xD3A89B: "BPT (Begin Presentation Text)",
    0xD3A99B: "EPT (End Presentation Text)",
    0xD3EE9B: "PTX (Presentation Text Data)",
    0xD3A69B: "PTD (Presentation Text Descriptor, format 1)",
    0xD3B19B: "PTD (Presentation Text Descriptor, format 2)",
    0xD3A79B: "CTC (Composed Text Control)",
    0xD3A8FB: "BIM (Begin Image)",
    0xD3A9FB: "EIM (End Image)",
    0xD3A6FB: "IDD (Image Data Descriptor)",
    0xD3EEFB: "IPD (Image Picture Data)",
    0xD3A87B: "BII (Begin IM Image Object)",
    0xD3A97B: "EII (End IM Image Object)",
    0xD3A67B: "IID (IM Image Input Descriptor)",
    0xD3A77B: "IOC (IM Image Output Control)",
    0xD3AC7B: "ICP (IM Image Cell Position)",
    0xD3EE7B: "IRD (IM Image Raster Data)",
    0xD3A8BB: "BGR (Begin Graphics)",
    0xD3A9BB: "EGR (End Graphics)",
    0xD3A6BB: "GDD (Graphics Data Descriptor)",
    0xD3EEBB: "GAD (Graphics Data)",
    0xD3A8EB: "BBC (Begin Bar Code)",
    0xD3A9EB: "EBC (End Bar Code)",
    0xD3A6EB: "BDD (Bar Code Data Descriptor)",
    0xD3EEEB: "BDA (Bar Code Data)",
    0xD3A892: "BOC (Begin Object Container)",
    0xD3A992: "EOC (End Object Container)",
    0xD3EE92: "OCD (Object Container Data)",
    0xD3ABC3: "MDR (Map Data Resource)",
    0xD3AFC3: "IOB (Include Object)",
    0xD3A8C6: "BRG (Begin Resource Group)",
    0xD3A9C6: "ERG (End Resource Group)",
    0xD3A8CE: "BRS (Begin Resource)",
    0xD3A9CE: "ERS (End Resource)",
    0xD3A85F: "BPS (Begin Page Segment)",
    0xD3A95F: "EPS (End Page Segment)",
    0xD3AF5F: "IPS (Include Page Segment)",
    0xD3A8DF: "BMO (Begin Overlay)",
    0xD3A9DF: "EMO (End Overlay)",
    0xD3AFD8: "IPO (Include Page Overlay)",
    0xD3ABD8: "MPO (Map Page Overlay)",
    0xD3A8CD: "BFM (Begin Form Map)",
    0xD3A9CD: "EFM (End Form Map)",
    0xD3A8CC: "BMM (Begin Medium Map)",
    0xD3A9CC: "EMM (End Medium Map)",
    0xD3ABCC: "IMM (Invoke Medium Map)",
    0xD3A8C5: "BFG (Begin Form Environment Group)",
    0xD3A9C5: "EFG (End Form Environment Group)",
    0xD3A6C5: "FGD (Form Environment Group Descriptor)",
    0xD3A6AF: "PGD (Page Descriptor)",
    0xD3ACAF: "PGP (Page Position, format 1)",
    0xD3B1AF: "PGP (Page Position, format 2)",
    0xD3A688: "MDD (Medium Descriptor)",
    0xD3A288: "MCC (Medium Copy Count)",
    0xD3A788: "MMC (Medium Modification Control)",
    0xD3A8C4: "BDG (Begin Document Environment Group)",
    0xD3A9C4: "EDG (End Document Environment Group)",
    0xD3A8C7: "BOG (Begin Object Environment Group)",
    0xD3A9C7: "EOG (End Object Environment Group)",
    0xD3A8A7: "BDI (Begin Document Index)",
    0xD3A9A7: "EDI (End Document Index)",
    0xD3A66B: "OBD (Object Area Descriptor)",
    0xD3AC6B: "OBP (Object Area Position)",
    0xD3ABEB: "MBC (Map Bar Code Object)",
    0xD3ABFB: "MIO (Map IO Image Object)",
    0xD3ABBB: "MGO (Map Graphics Object)",
    0xD3A889: "BFN (Begin Font)",
    0xD3A989: "EFN (End Font)",
    0xD3A789: "FNC (Font Control)",
    0xD3A689: "FND (Font Descriptor)",
    0xD3AE89: "FNO (Font Orientation)",
    0xD3AC89: "FNP (Font Position)",
    0xD38C89: "FNI (Font Index)",
    0xD3A289: "FNM (Font Patterns Map)",
    0xD3EE89: "FNG (Font Patterns)",
    0xD3AB89: "FNN (Font Name Map)",
    0xD3AB8A: "MCF (Map Coded Font, format 2)",
    0xD3B18A: "MCF (Map Coded Font, format 1)",
    0xD3A88A: "BCF (Begin Coded Font)",
    0xD3A98A: "ECF (End Coded Font)",
    0xD3A78A: "CFC (Coded Font Control)",
    0xD38C8A: "CFI (Coded Font Index)",
    0xD3A887: "BCP (Begin Code Page)",
    0xD3A987: "ECP (End Code Page)",
    0xD3A787: "CPC (Code Page Control)",
    0xD3A687: "CPD (Code Page Descriptor)",
    0xD38C87: "CPI (Code Page Index)",
    0xD3ABAF: "MPG (Map Page)",
    0xD3B15F: "MPS (Map Page Segment)",
    0xD3A090: "TLE (Tag Logical Element)",
    0xD3EEEE: "NOP (No Operation)",
    0xD3A8A5: "BPF (Begin Print File)",
    0xD3A9A5: "EPF (End Print File)",
}

# Begin/End structured fields usually start their data with an 8-byte
# EBCDIC token name.
_NAMED_TYPE_CODES = (0xA8, 0xA9)


@dataclass
class StructuredField:
    """One MO:DCA structured field, as found in the file."""

    offset: int
    sf_id: int
    flags: int
    sequence: int
    data: bytes

    @property
    def type_code(self) -> int:
        """The MO:DCA type code (Begin, End, Data, ...)."""
        return (self.sf_id >> 8) & 0xFF

    @property
    def category_code(self) -> int:
        """The MO:DCA category code (Document, Page, Image, ...)."""
        return self.sf_id & 0xFF

    @property
    def name(self) -> str:
        """Human-readable abbreviation, e.g. 'BPG (Begin Page)'."""
        return SF_NAMES.get(self.sf_id, f"Unknown (0x{self.sf_id:06X})")

    @property
    def token_name(self) -> Optional[str]:
        """The 8-byte EBCDIC object name on Begin/End fields, if present."""
        if self.type_code not in _NAMED_TYPE_CODES or len(self.data) < 8:
            return None
        try:
            return self.data[:8].decode("cp500").strip()
        except UnicodeDecodeError:
            return None


class AfpParseError(Exception):
    """Raised when a file does not parse as AFP."""


def iter_fields(data: bytes) -> Iterator[StructuredField]:
    """Yield every structured field in an AFP byte stream.

    Args:
        data: The raw bytes of an AFP file.

    Raises:
        AfpParseError: If the stream is malformed (bad carriage-control
            byte or a field that runs past the end of the data).
    """
    pos = 0
    while pos < len(data):
        if data[pos] != CARRIAGE_CONTROL:
            raise AfpParseError(
                f"expected 0x5A carriage-control byte at offset {pos}, "
                f"found 0x{data[pos]:02X}"
            )
        if pos + 9 > len(data):
            raise AfpParseError(f"truncated structured field at offset {pos}")
        length = int.from_bytes(data[pos + 1 : pos + 3], "big")
        if length < 8 or pos + 1 + length > len(data):
            raise AfpParseError(
                f"structured field at offset {pos} has bad length {length}"
            )
        yield StructuredField(
            offset=pos,
            sf_id=int.from_bytes(data[pos + 3 : pos + 6], "big"),
            flags=data[pos + 6],
            sequence=int.from_bytes(data[pos + 7 : pos + 9], "big"),
            data=bytes(data[pos + 9 : pos + 1 + length]),
        )
        pos += 1 + length


def parse_file(path: str) -> List[StructuredField]:
    """Parse an AFP file from disk into a list of structured fields."""
    with open(path, "rb") as handle:
        return list(iter_fields(handle.read()))
