"""Comparable layout geometry from an AFP page model and its PDF twin.

The fop-pairs ship a PDF rendered from the *same* XSL-FO as the AFP, so the
PDF is ground truth. This module pulls the few robust, font-independent
anchors that let the PDF act as an oracle for vertical layout fidelity:
text baselines and filled-rectangle (rule/bar) tops, expressed in **points,
measured from the top of the page** — the same frame the AFP page model uses
once L-units are scaled by 72/units_per_inch.

Used by tools/_compare_pair.py (printed report) and tests/test_fop_pairs.py
(asserted within tolerance). The PDF reader is deliberately minimal: classic,
non-object-stream PDFs as produced by Apache FOP — enough to walk the page
tree, decompress each page's content stream, and recover baselines and
rectangle tops. It is a test/dev oracle, not a general PDF parser.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

Matrix = Tuple[float, float, float, float, float, float]
_DEFAULT_PAGE_HEIGHT = 841.889  # A4 pt; only a fallback if no MediaBox


@dataclass
class PageGeom:
    """One page's comparable anchors, in points from the top of the page."""

    page_height: float
    baselines: List[float]  # distinct text-baseline y, sorted top→bottom
    rule_tops: List[float]  # distinct filled-rectangle top edges, sorted
    sizes: List[float]      # distinct text point sizes used on the page

    @property
    def first_baseline(self) -> Optional[float]:
        return self.baselines[0] if self.baselines else None

    @property
    def last_baseline(self) -> Optional[float]:
        return self.baselines[-1] if self.baselines else None

    @property
    def top_rule(self) -> Optional[float]:
        return self.rule_tops[0] if self.rule_tops else None

    @property
    def height(self) -> Optional[float]:
        """Top anchor (top rule, else first baseline) → last baseline."""
        if not self.baselines:
            return None
        top = self.rule_tops[0] if self.rule_tops else self.baselines[0]
        return self.last_baseline - top


def _distinct(values, eps: float = 0.6) -> List[float]:
    out: List[float] = []
    for v in sorted(values):
        if not out or abs(v - out[-1]) > eps:
            out.append(round(v, 2))
    return out


# --------------------------------------------------------------------------
# AFP side: straight off the product's page model.
# --------------------------------------------------------------------------
def afp_geometry(page) -> PageGeom:
    """Geometry of one readafp ``Page`` in PDF-comparable units."""
    scale = 72.0 / page.units_per_inch
    baselines = _distinct(t.y * scale for t in page.texts)
    tops = []
    for r in page.rules:
        # The PDF oracle captures only filled rectangles (colored bars / cell
        # backgrounds); FOP draws thin black table borders as *stroked* lines,
        # which we don't parse. So compare colored fills only — counting black
        # grid-line rules here would pit AFP borders against PDF fills.
        if (r.color or "").lower() in ("#000000", "black"):
            continue
        # The rule's rectangle extends by ``thickness`` along the B axis for a
        # horizontal (I) rule; its top edge is the smaller B coordinate.
        top = min(r.y, r.y + r.thickness) if r.axis == "I" else r.y
        tops.append(top * scale)
    sizes = _distinct(t.font_size * scale for t in page.texts)
    return PageGeom(page.height * scale, baselines, _distinct(tops), sizes)


# --------------------------------------------------------------------------
# PDF side: minimal classic-PDF reader + content-stream interpreter.
# --------------------------------------------------------------------------
def _mul(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _origin(m: Matrix) -> Tuple[float, float]:
    return m[4], m[5]


def _tokens(s: str) -> Iterator[Tuple[str, object]]:
    """Yield ('num', float) | ('op', str) | ('str'|'arr'|'dict', None).

    Strings, arrays and dicts are emitted as opaque tokens so their bytes
    never masquerade as operators/operands.
    """
    i, n = 0, len(s)
    delim = set(" \t\r\n\f()<>[]{}/%")
    while i < n:
        c = s[i]
        if c in " \t\r\n\f":
            i += 1
        elif c == "%":  # comment to end of line
            while i < n and s[i] not in "\r\n":
                i += 1
        elif c == "(":  # literal string, balanced parens, backslash escapes
            depth, i = 1, i + 1
            while i < n and depth:
                if s[i] == "\\":
                    i += 2
                    continue
                depth += (s[i] == "(") - (s[i] == ")")
                i += 1
            yield ("str", None)
        elif c == "<" and i + 1 < n and s[i + 1] == "<":
            i += 2
            yield ("dict", None)
        elif c == ">" and i + 1 < n and s[i + 1] == ">":
            i += 2
        elif c == "<":  # hex string
            i = s.find(">", i) + 1 or n
            yield ("str", None)
        elif c == "[":
            depth, i = 1, i + 1
            while i < n and depth:  # arrays may contain strings
                if s[i] == "(":
                    while i < n and s[i] != ")":
                        i += 2 if s[i] == "\\" else 1
                depth += (s[i] == "[") - (s[i] == "]")
                i += 1
            yield ("arr", None)
        elif c == "/":  # name
            i += 1
            while i < n and s[i] not in delim:
                i += 1
            yield ("name", None)
        elif c in "+-.0123456789":
            j = i + 1
            while j < n and s[j] in "+-.0123456789eE":
                j += 1
            try:
                yield ("num", float(s[i:j]))
            except ValueError:
                pass
            i = j
        else:
            j = i + 1
            while j < n and s[j] not in delim:
                j += 1
            yield ("op", s[i:j])
            i = j


def _interpret(content: str, page_height: float) -> PageGeom:
    ctm: Matrix = (1, 0, 0, 1, 0, 0)
    stack: List[Matrix] = []
    tm: Matrix = (1, 0, 0, 1, 0, 0)
    tlm: Matrix = (1, 0, 0, 1, 0, 0)
    leading = 0.0
    font_size = 0.0
    nums: List[float] = []
    pending_rect: Optional[Tuple[float, float]] = None  # (top_dev, bot_dev)
    baselines: List[float] = []
    rule_tops: List[float] = []
    sizes: List[float] = []

    def from_top(y: float) -> float:
        return page_height - y

    for kind, val in _tokens(content):
        if kind == "num":
            nums.append(val)
            continue
        if kind != "op":
            nums = []
            continue
        op = val
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ctm
        elif op == "cm" and len(nums) >= 6:
            ctm = _mul(tuple(nums[-6:]), ctm)
        elif op == "re" and len(nums) >= 4:
            x, y, w, h = nums[-4:]
            p0 = (ctm[0] * x + ctm[2] * y + ctm[4],
                  ctm[1] * x + ctm[3] * y + ctm[5])
            p1 = (ctm[0] * x + ctm[2] * (y + h) + ctm[4],
                  ctm[1] * x + ctm[3] * (y + h) + ctm[5])
            pending_rect = (from_top(max(p0[1], p1[1])),
                            from_top(min(p0[1], p1[1])))
        elif op in ("f", "F", "b", "B", "f*", "b*", "B*"):
            if pending_rect is not None:
                rule_tops.append(pending_rect[0])
            pending_rect = None
        elif op in ("n", "S", "s"):
            pending_rect = None
        elif op == "BT":
            tm = tlm = (1, 0, 0, 1, 0, 0)
        elif op == "Tm" and len(nums) >= 6:
            tm = tlm = tuple(nums[-6:])
        elif op == "Td" and len(nums) >= 2:
            tlm = _mul((1, 0, 0, 1, nums[-2], nums[-1]), tlm)
            tm = tlm
        elif op == "TD" and len(nums) >= 2:
            leading = -nums[-1]
            tlm = _mul((1, 0, 0, 1, nums[-2], nums[-1]), tlm)
            tm = tlm
        elif op == "TL" and nums:
            leading = nums[-1]
        elif op == "Tf" and nums:
            font_size = nums[-1]
        elif op == "T*":
            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            tm = tlm
        elif op in ("Tj", "TJ", "'", '"'):
            if op in ("'", '"'):  # move to next line, then show
                tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
            ox, oy = _origin(_mul(tm, ctm))
            baselines.append(from_top(oy))
            # The text matrix can scale the nominal Tf size (FOP rarely does);
            # the vertical scale of Tm·CTM gives the rendered size.
            scale = abs(_mul(tm, ctm)[3]) or 1.0
            sizes.append(font_size * scale)
        nums = []

    return PageGeom(
        page_height, _distinct(baselines), _distinct(rule_tops),
        _distinct(sizes),
    )


def _inflate(stream: bytes) -> bytes:
    try:
        return zlib.decompress(stream)
    except zlib.error:
        return stream


def _obj_body(raw: bytes, num: int) -> bytes:
    m = re.search(
        (r"\b%d\s+0\s+obj\b(.*?)\bendobj" % num).encode(), raw, re.S
    )
    return m.group(1) if m else b""


def _stream_bytes(raw: bytes, num: int) -> bytes:
    m = re.search(
        (r"\b%d\s+0\s+obj\b.*?stream\r?\n(.*?)\r?\nendstream" % num).encode(),
        raw,
        re.S,
    )
    return _inflate(m.group(1)) if m else b""


def _refs(blob: bytes) -> List[int]:
    return [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", blob)]


def _page_height(body: bytes, raw: bytes) -> float:
    for src in (body, raw):
        m = re.search(rb"/MediaBox\s*\[([^\]]+)\]", src)
        if m:
            nums = [float(x) for x in m.group(1).split()]
            if len(nums) == 4:
                return nums[3] - nums[1]
    return _DEFAULT_PAGE_HEIGHT


def pdf_geometries(pdf_path) -> List[PageGeom]:
    """Per-page :class:`PageGeom` for a classic FOP-style PDF, in page order."""
    raw = Path(pdf_path).read_bytes()
    kids_m = re.search(rb"/Type\s*/Pages\b.*?/Kids\s*\[([^\]]*)\]", raw, re.S)
    page_nums = _refs(kids_m.group(1)) if kids_m else _refs(
        b"".join(re.findall(rb"/Type\s*/Page\b[^s]", raw)) or b""
    )
    geoms: List[PageGeom] = []
    for pnum in page_nums:
        body = _obj_body(raw, pnum)
        cm = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if cm:
            content_nums = [int(cm.group(1))]
        else:  # /Contents [ a 0 R b 0 R ]
            am = re.search(rb"/Contents\s*\[([^\]]*)\]", body)
            content_nums = _refs(am.group(1)) if am else []
        content = b"\n".join(_stream_bytes(raw, c) for c in content_nums)
        geoms.append(
            _interpret(content.decode("latin-1", "replace"),
                       _page_height(body, raw))
        )
    return geoms
