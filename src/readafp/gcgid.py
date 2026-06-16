"""Bridge an external code page to an embedded font's glyphs via GCGID.

When a document's coded font names an *external* code page (e.g. cp1140 /
T1001140) we have the Python codec — byte → Unicode — but not the code
page's byte → GCGID map, which normally lives in the (absent) code-page
resource. Yet the file may still embed the *character set* (the FOCA font),
whose glyphs are keyed by IBM Graphic Character Global Identifiers (GCGIDs).

This module reconstructs the missing byte → GCGID map from the codec plus
the GCGID naming rules, so embedded glyphs can be drawn for external-code-
page text instead of a substitute font. Only mappings that are
*authoritative* are produced — algorithmic GCGID names and the standard
Latin/digit identifiers, each confirmed against the codec character — so a
byte is never silently mapped to the wrong glyph. Bytes whose GCGID the
rules can't derive (or the font doesn't carry) are left unmapped, and the
caller falls back to substitute text.

GCGID naming (IBM Character Data Representation Architecture, FOCA
reference Figure 56 "EBCDIC Code Page 500 With Character Set 103"):

* ``UNICxxxx`` — the Unicode scalar U+xxxx (algorithmic, exact).
* ``L{c}010000`` / ``L{c}020000`` — Latin letter ``c`` upper / lower case
  (verified against the embedded Sample 1 fonts: all 52 present).
* ``ND0n0000`` for digit ``n`` (1-9); ``ND100000`` for ``0``.
* ``SP010000`` — the space character (confirmed by Sample 1's embedded
  code page, where EBCDIC X'40' maps to SP010000).

Punctuation and accented-letter GCGIDs (``SM*``/``SD*``/``SC*``/``SA*`` and
the ``L..17..`` accent variants) are *not* derived here: their assignments
can't be read unambiguously from the spec figure, and guessing would risk
drawing the wrong glyph. They simply don't map.
"""

import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


def gcgid_for_char(ch: str) -> Optional[str]:
    """Return the authoritative GCGID for a single character, or None.

    Covers Latin letters, digits, the space, and the algorithmic
    ``UNICxxxx`` identifier for any other Unicode scalar in the BMP. The
    caller still checks the result against the font's actual glyph set.
    """
    if len(ch) != 1:
        return None
    if ch == " ":
        return "SP010000"
    if "A" <= ch <= "Z":
        return f"L{ch}010000"
    if "a" <= ch <= "z":
        return f"L{ch.upper()}020000"
    if "0" <= ch <= "9":
        n = ord(ch) - ord("0")
        return "ND100000" if n == 0 else f"ND0{n}0000"
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
