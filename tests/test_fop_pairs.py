"""AFP-render vs PDF-ground-truth geometry oracle for the fop-pairs.

Each fop-pair's PDF was rendered from the *same* XSL-FO as the AFP, so its
text baselines and rule/bar tops are ground truth for our render's vertical
layout — and, being positional, they are independent of which substitute
font we draw with. For every pair we compare a few font-independent anchors
(first/last text baseline, top rule) per page within a small tolerance.

Pairs with a known, not-yet-debugged gap are ``xfail``-ed with a specific
reason; as each pair is debugged and fixed, remove its marker (a strict
xfail turns into a failure if it starts passing, which is the reminder).
"""

from pathlib import Path

import pytest

from readafp.parser import iter_fields
from readafp.ptoca import extract_pages

from pair_geometry import afp_geometry, pdf_geometries

PAIRS = Path(__file__).parent.parent / "testdata" / "fop-pairs"
TOL = 2.0  # points; clean pairs agree within ~0.2pt
SIZE_TOL = 2.0  # points; heading sizes decode exact, textdeko scaled ≤1.9


def _todo(reason: str):
    return pytest.mark.xfail(reason=reason, strict=True)


# Order: the four that already match, then the four with open findings.
CASES = [
    "simple",
    "normal",
    "textdeko",
    "images",
    "table",
    # Not a render bug: FOP's AFP output has no ZapfDingbats/Symbol font, so it
    # dropped those specimen glyphs to X'3F' substitutes and wrapped the rows
    # differently than its PDF output (AFP 33 lines vs PDF 35). Ground-truth
    # divergence between FOP's two renderers — confirmed in a commercial AFP
    # viewer (only the 4 text fonts + 2 code pages are flagged missing, no
    # symbol font). Non-strict: it will never match, and that's correct.
    pytest.param(
        "fonts",
        marks=pytest.mark.xfail(
            reason="FOP AFP lacks ZapfDingbats/Symbol fonts (X'3F' substitutes); "
            "AFP/PDF wrap the symbol specimen to different line counts — "
            "ground-truth divergence, not a render bug",
            strict=False,
        ),
    ),
    # Not a render bug: FOP's PDF renderer paginates/wraps the justified list
    # bodies onto one more page than its AFP renderer. readAFP matches the
    # commercial AFPviewer exactly (12 pages, all content) — confirmed against
    # AFPviewer screenshots. See test_list_content_complete for the real guard.
    pytest.param(
        "list",
        marks=pytest.mark.xfail(
            reason="FOP AFP=12 pages vs PDF=13 (FOP paginates the two outputs "
            "differently); readAFP matches AFPviewer at 12 pages",
            strict=False,
        ),
    ),
    # Same FOP pagination divergence as `list`: AFP=9 pages, PDF=10. readAFP
    # matches AFPviewer at 9 pages with all content. See
    # test_readme_content_complete for the guard.
    pytest.param(
        "readme",
        marks=pytest.mark.xfail(
            reason="FOP AFP=9 pages vs PDF=10 (FOP paginates the two outputs "
            "differently); readAFP matches AFPviewer at 9 pages",
            strict=False,
        ),
    ),
]


def test_list_content_complete() -> None:
    """`list` paginates differently from its PDF (FOP), so the geometry test
    xfails — but the AFP content must stay complete. This guards that: 12
    pages (matching AFPviewer), no truncation, and all 17 nested-list items.
    """
    import re

    afp = PAIRS / "list.afp"
    if not afp.exists():
        pytest.skip("fop-pairs not present")
    pages = extract_pages(list(iter_fields(afp.read_bytes())), "cp500")
    assert len(pages) == 12  # matches the AFPviewer page count
    assert not any(p.truncated for p in pages)
    text = " ".join(t.text for p in pages for t in p.texts)
    items = {int(x) for x in re.findall(r"(\d+)\.\)", text)}
    assert items == set(range(1, 18))  # numbered list items 1..17


def test_readme_content_complete() -> None:
    """`readme` paginates differently from its PDF (FOP), so the geometry test
    xfails — guard that the AFP content stays complete: 9 pages (matching
    AFPviewer), no truncation, and the FOP-doc sections present.
    """
    afp = PAIRS / "readme.afp"
    if not afp.exists():
        pytest.skip("fop-pairs not present")
    pages = extract_pages(list(iter_fields(afp.read_bytes())), "cp500")
    assert len(pages) == 9  # matches the AFPviewer page count
    assert not any(p.truncated for p in pages)
    text = " ".join(t.text for p in pages for t in p.texts)
    for section in ("What is FOP", "Downloading FOP", "Running FOP"):
        assert section in text


@pytest.mark.parametrize("name", CASES)
def test_geometry_matches_pdf(name: str) -> None:
    afp = PAIRS / f"{name}.afp"
    pdf = PAIRS / f"{name}.pdf"
    if not afp.exists() or not pdf.exists():
        pytest.skip("fop-pairs not present")
    apages = extract_pages(list(iter_fields(afp.read_bytes())), "cp500")
    pdfs = pdf_geometries(str(pdf))
    assert len(apages) == len(pdfs), (
        f"{name}: page count {len(apages)} (AFP) vs {len(pdfs)} (PDF)"
    )
    for i, (ap, pg) in enumerate(zip(apages, pdfs), 1):
        ag = afp_geometry(ap)
        if ag.first_baseline is not None and pg.first_baseline is not None:
            assert abs(ag.first_baseline - pg.first_baseline) <= TOL, (
                f"{name} p{i} first baseline {ag.first_baseline} vs "
                f"{pg.first_baseline}"
            )
        if ag.last_baseline is not None and pg.last_baseline is not None:
            assert abs(ag.last_baseline - pg.last_baseline) <= TOL, (
                f"{name} p{i} last baseline {ag.last_baseline} vs "
                f"{pg.last_baseline}"
            )
        if ag.top_rule is not None and pg.top_rule is not None:
            assert abs(ag.top_rule - pg.top_rule) <= TOL, (
                f"{name} p{i} top rule {ag.top_rule} vs {pg.top_rule}"
            )
        # Every point size the PDF uses must be reproduced by some run on the
        # AFP page (decoded from the coded-font name) — catches headings that
        # silently fall back to the default body size.
        if ag.sizes:
            for ps in pg.sizes:
                assert any(abs(ps - a) <= SIZE_TOL for a in ag.sizes), (
                    f"{name} p{i} PDF size {ps}pt not reproduced "
                    f"(AFP sizes {ag.sizes})"
                )
