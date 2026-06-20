"""Side-by-side AFP-render vs PDF-ground-truth screenshots for a fop-pair.

Temporary dev tool (delete after the fop-pair debugging round). Renders each
page of testdata/fop-pairs/<name>.afp through readAFP's own SVG pipeline,
screenshots it with Playwright (msedge channel), screenshots the paired
<name>.pdf in the same browser's built-in PDF viewer, and writes both into the
gitignored comparison/ directory for eyeball diffing.

Usage:
    python tools/_compare_pair.py simple
    python tools/_compare_pair.py            # all pairs

No dev server required. Windows console is cp1252 — output stays ASCII.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from readafp.parser import iter_fields  # noqa: E402
from readafp.ptoca import extract_pages  # noqa: E402
from readafp.render import page_to_svg  # noqa: E402
from pair_geometry import afp_geometry, pdf_geometries  # noqa: E402

PAIRS_DIR = ROOT / "testdata" / "fop-pairs"
OUT_DIR = ROOT / "comparison"
FALLBACK_CODEPAGE = "cp500"  # fop-pairs label fonts T1V10500 -> cp500


def _svg_html(page) -> str:
    """Wrap one page's SVG in an HTML doc sized at ~96 dpi from its L-units."""
    px_w = round(page.width / page.units_per_inch * 96)
    px_h = round(page.height / page.units_per_inch * 96)
    svg = page_to_svg(page)
    # Force a concrete pixel size so the screenshot is page-sized.
    svg = svg.replace(
        "<svg ",
        f'<svg width="{px_w}" height="{px_h}" ',
        1,
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff}</style>"
        f"</head><body>{svg}</body></html>"
    )


def compare(name: str, browser) -> None:
    afp = PAIRS_DIR / f"{name}.afp"
    pdf = PAIRS_DIR / f"{name}.pdf"
    if not afp.exists():
        print("SKIP %s: no .afp" % name)
        return
    fields = list(iter_fields(afp.read_bytes()))
    pages = extract_pages(fields, FALLBACK_CODEPAGE)
    print("%s: %d page(s)" % (name, len(pages)))

    afp_pngs = []
    page = browser.new_page()
    for i, pg in enumerate(pages, 1):
        html_path = OUT_DIR / f"{name}_afp_p{i}.html"
        html_path.write_text(_svg_html(pg), encoding="utf-8")
        page.goto(html_path.as_uri())
        out = OUT_DIR / f"{name}_afp_p{i}.png"
        page.screenshot(path=str(out), full_page=True)
        afp_pngs.append(out)
        print("  wrote %s_afp_p%d.png" % (name, i))
    page.close()

    pdf_pngs = []
    if pdf.exists():
        n_pdf = len(pdf_geometries(str(pdf)))
        for n in range(1, n_pdf + 1):
            # A fresh page per PDF page so the #page=N fragment is honored on
            # load (Chromium's PDF viewer ignores a same-doc hash change).
            pp = browser.new_page()
            pp.set_viewport_size({"width": 1000, "height": 1300})
            pp.goto("%s#page=%d&zoom=page-fit" % (pdf.as_uri(), n))
            pp.wait_for_timeout(1200)  # let the viewer paint that page
            out = OUT_DIR / f"{name}_pdf_p{n}.png"
            pp.screenshot(path=str(out))
            pdf_pngs.append(out)
            pp.close()
        print("  wrote %d PDF page image(s)" % n_pdf)
    else:
        print("  (no PDF ground truth)")

    _geometry_report(name, pages, pdf)

    _stitch(name, afp_pngs, pdf_pngs, browser)


def _geometry_report(name, pages, pdf) -> None:
    """Print AFP vs PDF layout anchors (pt from page top) and deltas."""
    if not pdf.exists():
        return
    pdfs = pdf_geometries(str(pdf))
    if len(pages) != len(pdfs):
        print("  GEOMETRY: page count AFP=%d PDF=%d  <-- mismatch"
              % (len(pages), len(pdfs)))
    print("  GEOMETRY (pt from top; d = AFP-PDF):")
    print("    page | first base      | last base       | top rule")
    for i, (ap, pg) in enumerate(zip(pages, pdfs), 1):
        ag = afp_geometry(ap)

        def cell(a, b):
            if a is None or b is None:
                return "%-15s" % ("%s/%s" % (a, b))
            return "%6.1f/%6.1f d%+.1f" % (a, b, a - b)

        print("    %4d | %s | %s | %s" % (
            i,
            cell(ag.first_baseline, pg.first_baseline),
            cell(ag.last_baseline, pg.last_baseline),
            cell(ag.top_rule, pg.top_rule),
        ))


def _stitch(name, afp_pngs, pdf_pngs, browser) -> None:
    """Compose one labeled side-by-side PNG: our render(s) | PDF page(s).

    Done in the browser (no image libs): an HTML page lays the AFP page
    screenshots in a left column and the PDF page screenshots in a right
    column, page-for-page, then screenshots the whole thing.
    """
    def col(title, imgs):
        cells = "".join(
            f'<img src="{p.as_uri()}" '
            f'style="width:100%;display:block;border:1px solid #ccc;'
            f'margin-bottom:8px">'
            for p in imgs
        )
        return (
            '<div style="flex:1">'
            f'<div style="font:bold 16px sans-serif;margin:4px 0">{title}</div>'
            f"{cells}</div>"
        )

    left = col("readAFP render", afp_pngs)
    right = col("PDF ground truth", pdf_pngs)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{margin:0;padding:12px;background:#fff}"
        ".row{display:flex;gap:16px;align-items:flex-start}</style></head>"
        f"<body><div style='font:bold 18px sans-serif;margin-bottom:8px'>"
        f"{name}</div><div class='row'>{left}{right}</div></body></html>"
    )
    html_path = OUT_DIR / f"{name}_compare.html"
    html_path.write_text(html, encoding="utf-8")
    cp = browser.new_page()
    cp.set_viewport_size({"width": 1600, "height": 1000})
    cp.goto(html_path.as_uri())
    cp.wait_for_timeout(300)
    cp.screenshot(path=str(OUT_DIR / f"{name}_compare.png"), full_page=True)
    cp.close()
    print("  wrote %s_compare.png  <- open this one" % name)


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(exist_ok=True)
    names = sys.argv[1:] or [p.stem for p in sorted(PAIRS_DIR.glob("*.afp"))]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge")
        ctx = browser.new_context()
        for name in names:
            compare(name, ctx)
        browser.close()


if __name__ == "__main__":
    main()
