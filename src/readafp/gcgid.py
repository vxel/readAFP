"""Bridge an external code page to an embedded font's glyphs via GCGID.

When a document's coded font names an *external* code page (e.g. cp1140 /
T1001140) we have the Python codec — byte → Unicode — but not the code
page's byte → GCGID map, which normally lives in the (absent) code-page
resource. Yet the file may still embed the *character set* (the FOCA font),
whose glyphs are keyed by IBM Graphic Character Global Identifiers (GCGIDs).

This module reconstructs the missing byte → GCGID map from the codec plus
an authoritative character → GCGID table for IBM character set 103 (the
Latin set these fonts use), transcribed from FOCA reference Figure 56
("EBCDIC Code Page 500 With Character Set 103") and verified against the
``cp500`` codec — every cell's character matches. Because GCGIDs are
code-page-independent character identities, the same char → GCGID table
bridges any EBCDIC code page (cp1140, cp037, …): byte → Unicode (codec) →
GCGID. Only characters the embedded font actually carries are mapped, so a
byte is never drawn as the wrong glyph.

For characters outside CS103, the algorithmic ``UNICxxxx`` identifier
(U+xxxx) is tried as a fallback, since some fonts key extra glyphs that way
(e.g. ``UNICFB01`` for the fi ligature).
"""

import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# IBM character set 103: EBCDIC code page 500 byte -> GCGID, from FOCA
# reference Figure 56. Stored against cp500 code points; the character for
# each is taken from the cp500 codec at import (all 95 verified to match the
# figure). GCGIDs are character identities, so this yields a code-page-
# independent char -> GCGID map usable for any EBCDIC code page.
_CS103_CP500: Dict[int, str] = {
    0x40: "SP010000", 0x4A: "SM060000", 0x4B: "SP110000", 0x4C: "SA030000",
    0x4D: "SP060000", 0x4E: "SA010000", 0x4F: "SP020000",
    0x50: "SM030000", 0x5A: "SM080000", 0x5B: "SC030000", 0x5C: "SM040000",
    0x5D: "SP070000", 0x5E: "SP140000", 0x5F: "SD150000",
    0x60: "SP100000", 0x61: "SP120000", 0x6B: "SP080000", 0x6C: "SM020000",
    0x6D: "SP090000", 0x6E: "SA050000", 0x6F: "SP150000",
    0x79: "SD130000", 0x7A: "SP130000", 0x7B: "SM010000", 0x7C: "SM050000",
    0x7D: "SP050000", 0x7E: "SA040000", 0x7F: "SP040000",
    0x81: "LA010000", 0x82: "LB010000", 0x83: "LC010000", 0x84: "LD010000",
    0x85: "LE010000", 0x86: "LF010000", 0x87: "LG010000", 0x88: "LH010000",
    0x89: "LI010000",
    0x91: "LJ010000", 0x92: "LK010000", 0x93: "LL010000", 0x94: "LM010000",
    0x95: "LN010000", 0x96: "LO010000", 0x97: "LP010000", 0x98: "LQ010000",
    0x99: "LR010000",
    0xA1: "SD190000", 0xA2: "LS010000", 0xA3: "LT010000", 0xA4: "LU010000",
    0xA5: "LV010000", 0xA6: "LW010000", 0xA7: "LX010000", 0xA8: "LY010000",
    0xA9: "LZ010000",
    0xBB: "SM130000",
    0xC0: "SM110000", 0xC1: "LA020000", 0xC2: "LB020000", 0xC3: "LC020000",
    0xC4: "LD020000", 0xC5: "LE020000", 0xC6: "LF020000", 0xC7: "LG020000",
    0xC8: "LH020000", 0xC9: "LI020000",
    0xD0: "SM140000", 0xD1: "LJ020000", 0xD2: "LK020000", 0xD3: "LL020000",
    0xD4: "LM020000", 0xD5: "LN020000", 0xD6: "LO020000", 0xD7: "LP020000",
    0xD8: "LQ020000", 0xD9: "LR020000",
    0xE0: "SM070000", 0xE2: "LS020000", 0xE3: "LT020000", 0xE4: "LU020000",
    0xE5: "LV020000", 0xE6: "LW020000", 0xE7: "LX020000", 0xE8: "LY020000",
    0xE9: "LZ020000",
    0xF0: "ND100000", 0xF1: "ND010000", 0xF2: "ND020000", 0xF3: "ND030000",
    0xF4: "ND040000", 0xF5: "ND050000", 0xF6: "ND060000", 0xF7: "ND070000",
    0xF8: "ND080000", 0xF9: "ND090000",
}

# character -> GCGID, derived once from the cp500 layout above.
_CHAR_TO_GCGID: Dict[str, str] = {
    bytes([b]).decode("cp500"): gcgid for b, gcgid in _CS103_CP500.items()
}


def gcgid_for_char(ch: str) -> Optional[str]:
    """Return the authoritative GCGID for a single character, or None.

    Resolves the IBM CS103 Latin set (letters, digits, space, punctuation)
    from the verified Figure 56 table, then falls back to the algorithmic
    ``UNICxxxx`` identifier for any other Unicode scalar in the BMP. The
    caller still checks the result against the font's actual glyph set.
    """
    if len(ch) != 1:
        return None
    gcgid = _CHAR_TO_GCGID.get(ch)
    if gcgid is not None:
        return gcgid
    code = ord(ch)
    if 0 < code <= 0xFFFF:
        return f"UNIC{code:04X}"
    return None


def bridge_code_page(codec: str, font_gcgids: Set[str]) -> Dict[int, str]:
    """Build a byte → GCGID map for an external code page.

    For every byte the ``codec`` decodes to a character, derive that
    character's authoritative GCGID and keep it only when the embedded font
    actually carries that glyph. Returns ``{code_point: GCGID}``; an empty
    map means nothing could be bridged (caller falls back to substitute).
    """
    out: Dict[int, str] = {}
    for byte in range(256):
        try:
            ch = bytes([byte]).decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        gcgid = gcgid_for_char(ch)
        if gcgid and gcgid in font_gcgids:
            out[byte] = gcgid
    return out
