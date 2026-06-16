# readAFP

A web app for reading AFP (Advanced Function Presentation / MO:DCA) files.

Upload an AFP file and get a split-pane view: the full structured-field tree on
the left, an SVG render of each page on the right.  The two panes are linked —
clicking a field jumps the render to its page, paging the render scrolls the
inspector.

## Inspector

The structured-field tree reads in plain language, not just hex:

- **Readable "Field data" column** — each field is decoded to a one-line
  summary: PTX/NOP/TLE text, page/text/image geometry (PGD/PTD/IDD), font
  metrics (FND/FNC/FNP/FNO/FNI/FNM), code pages (CPD/CPC/CPI), and object
  maps (MCF/MIO/OBD/OBP/IPD), with a triplet rundown or hex as fallback. Full
  structured-field sizes match standard AFP inspectors.
- **Find** — search PTX text, NOP text (hidden comments/metadata), TLE tags,
  or field type, with case toggle, prev/next, and the match highlighted in
  place; a PTX hit also jumps and flashes the rendered text.
- **Multi-select filter** — click field-type counts to show several types at
  once; click a field to expand its decoded triplets.

## What renders

| Feature | Status |
|---|---|
| PTOCA presentation text — positions, colors, fonts (family/weight/size from MDR) | ✅ |
| Rules (lines, table borders, bands) | ✅ |
| MCF code-page auto-detection — EBCDIC decoded per font local-id | ✅ |
| IOCA raster images — bilevel, grayscale, JPEG, banded CMYK | ✅ |
| BCOCA bar codes — QR symbols generated with segno | ✅ |
| GOCA vector graphics — lines, boxes, arcs, Bézier curves, area fills | ✅ |
| FOCA raster fonts — embedded bitmap glyphs rendered as a specimen sheet | ✅ |
| Page overlays — BMO/EMO content composited onto pages via IPO | ✅ |
| Rotated text — STO orientation (0/90/180/270°) | ✅ |
| FOCA outline fonts — Adobe Type 1 (PFB) and CFF / CID-keyed (Type 2) glyphs rasterized to real shapes (metadata sheet only on decode failure) | ✅ |
| AFP resource files — page-less font/overlay/image resources flagged; standalone overlays & images rendered | ✅ |
| Missing-resource detection — names external code pages / character sets a document references but doesn't embed | ✅ |
| GOCA partial-arc and character-string orders | partial |
| Document text in the file's own embedded raster font (code page also embedded) | ✅ |
| Document text in the file's own embedded outline font (Type 1 / CFF, code page also embedded) | ✅ |
| True CFF / CID-keyed (Type 0) outline fonts — Type 2 charstrings | ✅ |
| Document text via *external* (non-embedded) code pages | ❌ |

## Run

```bash
pip install -r requirements.txt
python run.py
# open http://127.0.0.1:8770
```

Drop any `.afp` file on the page — or try one of the bundled samples:

| File | What it demonstrates |
|---|---|
| `testdata/sample1_health/01_Health_Coverage.afp` | Modern TrueType AFP — text, rules, fonts |
| `testdata/fop-pairs/` | Multi-page AFP with IOCA images (Apache FOP output) |
| `testdata/github-samples/afplib_ende.afp` | BCOCA QR bar code |
| `testdata/goca_sample.afp` | Synthetic GOCA sample — filled rect, zigzag, ellipse, Bézier |
| `testdata/foca_sample.afp` | Embedded raster fonts — Times-Roman & Courier glyph specimen |
| `testdata/bcoca_sample.afp` | Synthetic BCOCA QR bar code (repo URL) |
| `testdata/overlay_sample.afp` | Page overlay — letterhead/footer composited via IPO |

## Test

```bash
pytest          # 114 tests
pytest tests/test_goca.py -v   # GOCA decoder only
```

## Project layout

```
src/readafp/
  parser.py     # MO:DCA byte-stream parser → List[StructuredField]
  ptoca.py      # PTOCA decoder + page extraction → List[Page]
  render.py     # Page → SVG
  triplets.py   # MO:DCA triplet decoding + describe_field()
  ioca.py       # IOCA image segment decoder → PNG / JPEG / CMYK bands
  bcoca.py      # BCOCA bar code decoder → BarCode + QR PNG via segno
  goca.py       # GOCA drawing-order decoder → SVG fragment
  foca.py       # FOCA raster-font decoder → glyph bitmaps (PNG)
  app.py        # Flask app (POST /inspect)
  templates/index.html   # split-pane UI

tests/          # 114 pytest tests (one file per module)
testdata/
  sample1_health/        # modern TrueType AFP + PDF ground truth
  alpheus-corpus/        # 138 AFP files from alpheusafpparser test suite
  fop-pairs/             # 8 Apache FOP AFP+PDF pairs (IOCA images)
  github-samples/        # 16 Apache-2.0 files from afplib / Apache FOP
  goca_sample.afp        # hand-crafted GOCA test file (4 graphic objects)
tools/
  make_goca_sample.py    # generates goca_sample.afp
docs/
  specs/                 # official AFP Consortium reference PDFs
```

## GOCA rendering

GOCA (Graphics Object Content Architecture) encodes vector graphics as a stream
of binary drawing orders inside AFP `BGR`…`EGR` object brackets.  Each object
gets a `GDD` descriptor (specifying the GPS coordinate window and resolution)
and one or more `GAD` fields carrying the drawing-order bytes.

readAFP decodes the full drawing-order stream in `src/readafp/goca.py`:

- **Order dispatch** — `iter_orders()` parses the four drawing-order formats
  (fixed-1-byte, fixed-2-byte, long, and extended) and handles `Begin Segment`
  framing (0x70 command), skipping unchained segments.
- **Coordinate transform** — GPS Y increases upward; SVG Y increases downward.
  Every GPS point `(gx, gy)` maps to SVG `(gx − xlwind, ytwind − gy)` where
  `xlwind` and `ytwind` are the GPS window origin from the `GDD`.
- **Color** — `GSCOL` (palette index), `GSECOL` (OCA 2-byte table), and
  `GSPCOL` (process color — RGB and CMYK) are all supported.
- **Shapes** — `GLINE`/`GCLINE` (polylines), `GRLINE`/`GCRLINE` (relative
  lines), `GBOX`/`GCBOX` (rectangles), `GFARC`/`GCFARC` (full arcs rendered
  as SVG ellipses), `GPARC`/`GCPARC` (partial arcs via SVG arc paths),
  `GCBEZ`/`GCCBEZ` (cubic Bézier curves).
- **Area fills** — `GBAR`/`GEAR` accumulate sub-paths and emit a filled
  `<path>` with the current pattern symbol and color.
- **SVG embedding** — each GOCA object becomes a nested `<svg viewBox>` at
  its OBP-specified page position.  The `viewBox` maps GPS units to page
  L-units automatically, so no explicit scaling is needed in the markup.

No public AFP files with real GOCA drawing orders are known to exist in the
open-source world.  `testdata/goca_sample.afp` was generated by
`tools/make_goca_sample.py` using the same byte-building helpers as the test
suite, and exercises all four major shape types.

## AFP format primer

AFP files are sequences of **structured fields**: `0x5A` + u16 length + 3-byte
SF-ID + flags + sequence number + data.  SF-IDs follow the pattern
`0xD3 + type_code + category_code`.

Pages are bracketed by `BPG`/`EPG`.  Inside each page, objects are nested:

```
BPG (Begin Page)
  BAG/EAG     active environment group — PGD geometry, MCF fonts
  BPT/EPT     presentation text → PTX fields → PTOCA control sequences
  BIM/EIM     image object → IPD fields → IOCA segment data
  BBC/EBC     bar code object → BDD descriptor + BDA data
  BGR/EGR     graphics object → GDD descriptor + GAD drawing orders
EPG (End Page)
```

The coordinate unit is the **L-unit** — typically 1440 per inch (1 pt = 20
L-units), derived from `PGD`/`PTD` fields.  GOCA has its own GPS unit system
declared per-object in `GDD`.
