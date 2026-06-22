"""Aggregate AFP-vs-PDF geometry error across the fop-pairs.

The pass/fail oracle in ``tests/test_fop_pairs.py`` only checks three anchors
per page (first/last baseline, top rule) within a 2pt tolerance. That hides
residual error in every *interior* baseline, every rule top, and every text
size. This module turns the same PDF-as-oracle into a single continuous
number — the mean nearest-anchor distance, in points — so a loop can tell
whether a render change actually moved fidelity, not just whether it still
clears the coarse gate.

It deliberately reuses ``pair_geometry`` (the validated oracle) and only adds
a metric on top: for each comparable page it greedily matches each PDF anchor
to its nearest AFP anchor and averages the absolute gaps. The clean,
ground-truth-divergent pairs (fonts/list/readme) are reported but excluded
from the headline number, mirroring the xfail rationale in the test.

Run:  python tools/fidelity_error.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from readafp.parser import iter_fields
from readafp.ptoca import extract_pages

from pair_geometry import PageGeom, afp_geometry, pdf_geometries


def _all_rule_tops(page) -> List[float]:
    """Every rule's top edge in points-from-top, *including black rules*.

    ``afp_geometry`` deliberately drops black rules so a ``table``'s stroked
    borders (which the PDF reader can't see) don't pit AFP rules against
    nothing. But the fidelity metric matches each *PDF* anchor to its nearest
    AFP rule, so extra AFP rules can never inflate a gap — and keeping black
    rules fixes the ``textdeko`` case, where FOP draws those same rules as
    *filled* rectangles the PDF reader does capture. Symmetric, safe, honest.
    """
    scale = 72.0 / page.units_per_inch
    tops = []
    for r in page.rules:
        top = min(r.y, r.y + r.thickness) if r.axis == "I" else r.y
        tops.append(round(top * scale, 2))
    return sorted(set(tops))

PAIRS = Path(__file__).parent.parent / "testdata" / "fop-pairs"

# Pairs whose AFP/PDF differ in ground truth (FOP's two renderers diverge),
# per the xfail reasons in tests/test_fop_pairs.py. Measured but not in the
# headline number, so the loop is never rewarded for chasing FOP's own noise.
DIVERGENT = {"fonts", "list", "readme"}


def _nearest_mean(afp_vals: List[float], pdf_vals: List[float]) -> float:
    """Mean |gap| matching each PDF anchor to its nearest AFP anchor."""
    if not pdf_vals or not afp_vals:
        return 0.0
    total = 0.0
    for p in pdf_vals:
        total += min(abs(p - a) for a in afp_vals)
    return total / len(pdf_vals)


def page_error(ag: PageGeom, pg: PageGeom, rule_tops: List[float]) -> dict:
    return {
        "baselines": _nearest_mean(ag.baselines, pg.baselines),
        "rule_tops": _nearest_mean(rule_tops, pg.rule_tops),
        "sizes": _nearest_mean(ag.sizes, pg.sizes),
    }


def pair_error(name: str) -> dict | None:
    afp = PAIRS / f"{name}.afp"
    pdf = PAIRS / f"{name}.pdf"
    if not afp.exists() or not pdf.exists():
        return None
    apages = extract_pages(list(iter_fields(afp.read_bytes())), "cp500")
    pdfs = pdf_geometries(str(pdf))
    pages = []
    for ap, pg in zip(apages, pdfs):
        pages.append(page_error(afp_geometry(ap), pg, _all_rule_tops(ap)))
    n = max(len(pages), 1)
    agg = {
        k: sum(p[k] for p in pages) / n
        for k in ("baselines", "rule_tops", "sizes")
    }
    agg["overall"] = sum(agg.values()) / 3
    agg["afp_pages"] = len(apages)
    agg["pdf_pages"] = len(pdfs)
    return agg


def _pdf_text_x_per_page(pdf_path) -> List[List[float]]:
    """Per-page distinct text-origin x (device pt) for a classic FOP PDF.

    Reuses ``pair_geometry``'s validated tokenizer and matrix helpers without
    modifying the oracle — this is a horizontal complement to its vertical
    anchors, so a justification/indent error (which the vertical metric can't
    see) would show up as a left-edge x mismatch.
    """
    from pair_geometry import (
        _distinct, _mul, _obj_body, _origin, _refs, _stream_bytes, _tokens,
    )

    raw = Path(pdf_path).read_bytes()
    kids_m = re.search(rb"/Type\s*/Pages\b.*?/Kids\s*\[([^\]]*)\]", raw, re.S)
    page_nums = _refs(kids_m.group(1)) if kids_m else []
    out: List[List[float]] = []
    for pnum in page_nums:
        body = _obj_body(raw, pnum)
        cm = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if cm:
            content_nums = [int(cm.group(1))]
        else:
            am = re.search(rb"/Contents\s*\[([^\]]*)\]", body)
            content_nums = _refs(am.group(1)) if am else []
        content = b"\n".join(
            _stream_bytes(raw, c) for c in content_nums
        ).decode("latin-1", "replace")

        ctm = (1, 0, 0, 1, 0, 0)
        stack = []
        tm = tlm = (1, 0, 0, 1, 0, 0)
        leading = 0.0
        nums: List[float] = []
        xs: List[float] = []
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
            elif op == "BT":
                tm = tlm = (1, 0, 0, 1, 0, 0)
            elif op == "Tm" and len(nums) >= 6:
                tm = tlm = tuple(nums[-6:])
            elif op in ("Td", "TD") and len(nums) >= 2:
                if op == "TD":
                    leading = -nums[-1]
                tlm = _mul((1, 0, 0, 1, nums[-2], nums[-1]), tlm)
                tm = tlm
            elif op == "TL" and nums:
                leading = nums[-1]
            elif op == "T*":
                tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
            elif op in ("Tj", "TJ", "'", '"'):
                if op in ("'", '"'):
                    tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                    tm = tlm
                ox, _oy = _origin(_mul(tm, ctm))
                xs.append(ox)
            nums = []
        out.append(_distinct(xs))
    return out


def horizontal_report() -> None:
    """Compare leftmost text x (AFP vs PDF) per page for the clean pairs."""
    print("\nHorizontal (leftmost text x, pt):")
    print(f"{'pair':<10} {'page':>5} {'afp_x':>9} {'pdf_x':>9} {'gap':>7}")
    print("-" * 45)
    for name in ("simple", "normal", "textdeko", "images", "table"):
        afp = PAIRS / f"{name}.afp"
        pdf = PAIRS / f"{name}.pdf"
        if not afp.exists() or not pdf.exists():
            continue
        apages = extract_pages(list(iter_fields(afp.read_bytes())), "cp500")
        pdf_xs = _pdf_text_x_per_page(str(pdf))
        for i, (ap, pxs) in enumerate(zip(apages, pdf_xs), 1):
            if not ap.texts or not pxs:
                continue
            scale = 72.0 / ap.units_per_inch
            afp_x = min(t.x for t in ap.texts) * scale
            pdf_x = min(pxs)
            print(f"{name:<10} {i:>5} {afp_x:>9.2f} {pdf_x:>9.2f} "
                  f"{abs(afp_x - pdf_x):>7.2f}")


def main() -> None:
    names = [
        "simple", "normal", "textdeko", "images", "table",
        "fonts", "list", "readme",
    ]
    clean_overall = []
    print(f"{'pair':<10} {'pages':>9} {'baselns':>9} "
          f"{'rules':>9} {'sizes':>9} {'overall':>9}")
    print("-" * 60)
    for name in names:
        e = pair_error(name)
        if e is None:
            print(f"{name:<10} (missing)")
            continue
        tag = "  (divergent)" if name in DIVERGENT else ""
        pages = f"{e['afp_pages']}/{e['pdf_pages']}"
        print(f"{name:<10} {pages:>9} {e['baselines']:>9.3f} "
              f"{e['rule_tops']:>9.3f} {e['sizes']:>9.3f} "
              f"{e['overall']:>9.3f}{tag}")
        if name not in DIVERGENT:
            clean_overall.append(e["overall"])
    if clean_overall:
        headline = sum(clean_overall) / len(clean_overall)
        print("-" * 60)
        print(f"HEADLINE mean overall error (clean pairs): {headline:.4f} pt")
    horizontal_report()


if __name__ == "__main__":
    main()
