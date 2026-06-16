# readAFP — AFP Viewer/Inspector

Flask web app that parses, inspects, and renders AFP (MO:DCA) files to SVG.
Upload an AFP; get a split-pane view: structured-field tree on the left, SVG render on the right.

## Run & Test

```bash
python run.py          # http://127.0.0.1:8770
pytest                 # full suite
pytest tests/test_ptoca.py -v   # single module
```

No build step. Dependencies: `flask>=3.0`, `segno>=1.6`, stdlib only (zlib, struct, base64, codecs).

## Project Layout

```
src/readafp/
  parser.py    # MO:DCA byte-stream parser → List[StructuredField]
  ptoca.py     # PTOCA decoder + page extraction → List[Page]
  render.py    # Page → SVG string
  triplets.py  # MO:DCA triplet decoding + describe_field()
  ioca.py      # IOCA image segment decoder → IocaImage / PNG / JPEG
  bcoca.py     # BCOCA bar code decoder → BarCode + QR PNG via segno
  goca.py      # GOCA drawing-order decoder → GocaGraphic / SVG fragment
  foca.py      # FOCA raster-font decoder → Font / Glyph bitmaps (PNG)
  type1.py     # Adobe Type 1 (PFB) charstring interpreter → outline paths
  cff.py       # CFF / CID-keyed Type 2 charstring interpreter → outline paths
  gcgid.py     # external code page → embedded glyph bridge (byte→GCGID)
  app.py       # Flask app (POST /inspect), create_app()
  templates/index.html   # split-pane UI

tests/
  test_parser.py, test_ptoca.py, test_triplets.py,
  test_ioca.py, test_bcoca.py, test_app.py, test_foca.py, test_goca.py,
  test_cff.py, test_gcgid.py

testdata/
  sample1_health/     # modern TrueType AFP + PDF ground truth (1 page, 306 text runs)
  alpheus-corpus/     # 138 AFP files from alpheusafpparser test suite
  fop-pairs/          # 8 Apache FOP AFP+PDF pairs (multi-page, IOCA images)
  github-samples/     # 16 Apache-2.0 files from afplib and FOP
```

## AFP Structure Hierarchy

```
BDT (Begin Document)
  BRG…ERG  — resource group (fonts, images, overlays)
  BPG…EPG  — page
    BAG…EAG — active environment group
      PGD   — page geometry + units-per-inch
      PTD   — presentation text descriptor
      MCF   — Map Coded Font: local font id → EBCDIC codepage
      MDR   — Map Data Resource: local font id → FQN name + point size
    BPT…EPT / PTX  — presentation text object / control sequences
    BIM…EIM / IPD  — image object / image data
    BBC…EBC / BDD + BDA  — bar code object / descriptor + data
EDT (End Document)
```

## Key Domain Concepts

**L-units** — coordinate system unit; typically 1440/inch (1 pt = 20 L-units). FOP uses 240/inch. Always derived from PGD/PTD `units_per_inch`, never hardcoded.

**Structured fields** — fixed binary records: `0x5A` carriage-control, u16 length, 3-byte SF ID (`0xD3` + type_code + category_code), flags(1), sequence(2), data. Parsed by `iter_fields()`.

**PTOCA control sequences** — inside PTX data; escape `0x2B 0xD3` introduces a sequence, then length/type/params. Low bit of type = chained (next sequence follows without escape). Key sequences: AMI/RMI (inline position), AMB/RMB (baseline position), SBI (baseline increment), TRN (transparent text), SCFL (set font local id), STC/SEC (color), DIR/DBR (draw rule), STO (text orientation).

**Triplets** — self-identifying sub-parameters on structured fields: (u8 length, u8 id, data). Must tile their slot exactly. Key triplets: 0x02 FQN (Fully Qualified Name, with FQN type byte), 0x10 Object Classification, 0x82 Parameter Value.

**MCF/MDR** — MCF declares EBCDIC codepage per font local-id; MDR maps local-id to font family/weight/size via FQN triplets. Both parsed in `ptoca.py`; `parse_mcf_codepages()` also in `triplets.py`.

**TRN decoding** — if high byte of first two bytes is `0x00`, treat as UTF-16BE (TrueType). Otherwise decode as EBCDIC using the codepage for the active font (from MCF), falling back to the user-selected codepage.

**IOCA images** — BIM…EIM bracket; IPD fields carry concatenated self-defining fields (SDFs). Key SDFs: 0x94 Image Size, 0x95 Image Encoding, 0x96 IDE Size, 0xFE92 Image Data, 0xFE9C Band Image Data (CMYK planes). Compressions: 0x03 = uncompressed, 0x83 = JPEG. Bilevel inverted (IOCA 1 = mark/dark).

**CMYK composite** — four grayscale JPEG planes (one per ink). Renderer applies `feColorMatrix` filters to invert each plane to its complement color, then `mix-blend-mode: multiply` to optically compose: R=(1-C)(1-K), G=(1-M)(1-K), B=(1-Y)(1-K).

**BCOCA / QR** — BDD (descriptor) + BDA (data) bytes parsed by `parse_barcode()`. Only QR (`type=0x1C`) in corpus. BDA byte 5 bit 0 triggers EBCDIC→ASCII translation (codec from byte 6). Version = byte 7 (0 = auto), EC level = byte 8 (0-3 = L/M/Q/H). Symbol generated with `segno`.

## Data Model (ptoca.py)

```python
TextRun(x, y, text, color, font_id, font_size, font_family, font_weight, src)
Rule(x, y, length, thickness, axis, color, src)   # axis: "I"=horiz, "B"=vert
ImageRef(x, y, width, height, mime, data, bands, crisp)   # bands = CMYK list
Page(width, height, units_per_inch, texts, rules, images, truncated)
```

`Page.plain_text` joins text runs in reading order.

## SVG Rendering (render.py)

- `page_to_svg(page)` — builds SVG; viewBox in L-units.
- Rules: `<rect>` extending in +I (right) or +B (down) direction from position.
- Text: substitute fonts. The family comes from MDR, else from the
  embedded character set's typeface via `_substitute_font()` (COURIER →
  monospace, TIMES → serif, HELVETICA → sans), else Arial — so even when
  a char set's glyphs can't be drawn (external code page) its real metrics
  are approximated, which also curbs over-stretching. `textLength` +
  `lengthAdjust="spacing"` stretches a run to the AFP-implied width from
  the next run's position (`_fit()`), ratio-guarded against distortion.
- Images: `<image>` with base64 data URI. CMYK planes use `<filter>` + `mix-blend-mode: multiply`. Bilevel/barcode: `image-rendering: pixelated` (`crisp=True`).

## Rendering Fidelity Principle

Render only what the AFP contains. No invented features (no auto-linkified URLs, no assumed defaults beyond what PGD/MDR specify). If data is missing, skip or log — never fabricate.

## SF Type and Category Codes

Type codes: `0xA8`=Begin, `0xA9`=End, `0xA6`=Descriptor, `0xAB`=Map, `0xAC`=Position, `0xAF`=Include, `0xEE`=Data, `0xB1`=Migration.

Category codes: `0xA8`=Document, `0xAF`=Page, `0x9B`=Presentation Text, `0xFB`=Image, `0xBB`=Graphics, `0xEB`=Bar Code, `0x92`=Object Container, `0xC6`/`0xCE`=Resource.

## Corpus Notes

- `alpheus-corpus/`: 138 files, mostly structural shells; all parse without error.
- `sample1_health/`: primary render target — 1-page modern TrueType AFP with PDF ground truth.
- `fop-pairs/`: multi-page AFP with IOCA images; paired PDF ground truth for visual comparison.
- `github-samples/`: edge cases including FOCA raster fonts, unbracketed PTX, IOCA variants.

## FOCA (font objects)

Embedded raster fonts (BFN…EFN) are decoded in `foca.py`: FNI metrics
(GCGID, character increment, FNM index) join FNM box dimensions and FNG
1-bit-per-pel pattern data to rebuild each glyph as a PNG (FOCA toned
pel = 1, inverted to PNG's 0=black). Font-resource files with no
document pages render a specimen sheet (one page per raster font, glyph
grid labeled by GCGID) via `_font_specimen_pages()` in `ptoca.py`.

Outline fonts (Type 1 PFB / CFF / CID) have no bitmaps. The embedded font
program in the FNG is interpreted into outline paths: `type1.py` for Adobe
Type 1 PFB (PFB extraction, eexec + charstring decrypt, a Type 1
charstring interpreter with callsubr / flex / hint-replacement / `seac`
accent composition), and `cff.py` for CFF — a Type 2 charstring
interpreter over the CFF data structures (INDEX, Top/Private DICT,
charset, global+local Subrs, and for CID-keyed fonts the ROS marker plus
FDArray/FDSelect that pick each glyph's Private dict and subrs).
`_decode_outlines()` in `foca.py` sniffs the FNG (`_is_cff`: bare-CFF
`01 00` header or `OTTO` wrapper) to choose the interpreter, then decodes
each character's glyph keyed by GCGID, attaching `outline_glyphs` +
`units_per_em` to the `Font`. Both interpreters expose the same
`Glyph`/`glyph_to_path_d` shape (defined in `type1.py`).
`_outline_glyph_page()` then draws the **actual glyph shapes** as SVG
`<path>`s (one `VectorGraphic` per glyph, baseline-aligned, labeled by
name) — the font's true printed outline. If the outlines can't be
decoded (a decode failure), `_outline_font_page()`
falls back to a metadata sheet: typeface, the technology sniffed from the
FNG header (`%!PS-AdobeFont` ⇒ Adobe Type 1 PFB), the character count, and
a `GCGID · glyph name · increment` grid. FNI records repeat per rotation,
so `_dedup_orientations()` keeps one entry per GCGID and reports the
orientation count; `_fnn_glyph_names()` resolves each GCGID to its
readable name via the Font Name Map (FNN). Specimen text sets
`TextRun.fit = False` so the fixed grid is never width-fitted by
`render._fit()`. Decoded advance widths match the FNI increments exactly,
the independent oracle for interpreter correctness.

## Page Overlays (BMO/EMO + IPO)

`extract_pages` captures a `BMO…EMO` overlay like a page, keyed by its
EBCDIC name. A page's `IPO` (Include Page Overlay) field then composites
that overlay's text/rules/images/graphics onto the page via
`_include_overlay()`, shifted by the IPO X/Y offset (name = 8 EBCDIC
bytes + 3+3 signed offset) and scaled if the overlay declared a
different resolution. IPO references the overlay by name directly; MPO
local-id→name indirection is not yet handled.

## What's Not Yet Implemented

- FOCA outline fonts: both Adobe Type 1 (PFB, `type1.py`) and CFF /
  CID-keyed (Type 0, `cff.py`) glyph shapes ARE now rasterized to SVG
  paths for the specimen. `foca._decode_outlines` sniffs the FNG program
  (`_is_cff`) and dispatches to the Type 2 interpreter for bare-CFF or
  OTTO-wrapped programs, the Type 1 interpreter otherwise. CID fonts use
  the FDArray/FDSelect to pick each glyph's Private dict and local subrs.
  No CFF font exists in the corpus (the two PatTech X'1F' fonts embed
  Type 1 PFB), so `cff.py` is validated against fontTools as an
  independent oracle — see `tests/test_cff.py` and the fixtures built by
  `tools/make_cff_sample.py`. Both Type 1 and CFF outline glyphs are now
  wired into **document** text as well as the specimen (see Phase B below).
- Document text in the file's own fonts (Phase B) — done for both
  **raster** and **outline** fonts whose code page is also embedded. A TRN
  run resolves each byte through the embedded code page (CPI → GCGID) to a
  glyph in the embedded character set: raster glyphs draw as real bitmaps
  (`_emit_embedded_glyphs`); outline glyphs (Type 1 / CFF) draw as one
  `<path>` VectorGraphic per run, laid out in the font's design-unit space
  and scaled so the em maps to the run's point size, stepping by each
  glyph's **exact** design-unit advance (`_emit_embedded_outlines`). Both
  are fed by `_scan_code_pages` + `triplets.mcf_font_resources` and keyed
  by MCF local id. No corpus document uses an embedded outline font, so the
  outline path is validated against a synthetic fixture
  (`testdata/cff_document_sample.afp`, built by `make_cff_sample.py`) whose
  glyph shapes still trace to the fontTools-validated CFF outlines.

  **External code pages** (e.g. cp1140, the bulk of `Sample 1.afp`) are now
  bridged too: when the code page is *not* embedded but the character set
  *is*, `gcgid.bridge_code_page` reconstructs byte→GCGID from the codec
  (byte→Unicode) via an authoritative character→GCGID table for IBM
  character set 103, transcribed from FOCA reference **Figure 56** ("EBCDIC
  Code Page 500 With Character Set 103") — all 95 cells verified against the
  `cp500` codec (`gcgid._CS103_CP500`). Note lowercase is `L{c}010000`,
  uppercase `L{c}020000` (not the reverse). Outside CS103 it falls back to
  the algorithmic `UNICxxxx`. Only font-present mappings are produced, so a
  byte is never drawn as the wrong glyph. A run is drawn in embedded glyphs
  only when ≥ 70 % (`_EMBED_COVERAGE_MIN`) of its bytes resolve, else the
  whole run falls back to a substitute font — no half-bridged words. Runs
  drawn as glyphs record their decoded text in a hidden `Page.text_layer`
  so Copy-text / `.txt` export stays complete.

  Raster embedded glyphs are sized and spaced from real metrics:
  `foca.Font` carries the pattern **resolution** (FNC bytes 24-25, pels/10
  inch) and **point size** (FND bytes 34-35), so a pattern pel maps to
  `upi/resolution` L-units and the pen advances by each glyph's FNI
  **character increment** (1000/em) × the em in L-units — not the bitmap
  width. Each glyph's FNI **baseline offset** (bytes 12-13, 1000/em) drops
  descenders (g, p, q, y) below the line. With these three fixes Sample 1's
  body renders ~1900 real embedded TIMES-ROMAN/COURIER glyphs, readable and
  faithful (verified by Playwright screenshot against the substitute
  baseline).

  Still open: (a) embedded raster glyphs render in black — STC/SEC text
  color isn't applied to the 1-bit bitmaps; (b) only 0° orientation for
  embedded glyphs.
- FNI character-increment widths are not yet fed into document text
  fitting (render still uses position-anchored width estimation; the
  primary health-coverage sample embeds no fonts, so it cannot benefit).
- MPO (Map Page Overlay) local-id indirection — IPO-by-id files unhandled.
- GOCA character-string orders are not implemented. (Partial arcs are
  done: circular-arc sweep direction and rotated-ellipse orientation are
  both verified — `_handle_gparc` derives the ellipse semi-axes, x-axis
  rotation and sweep flag from the GSAP arc-parameter matrix, mirroring
  the full-arc path. Ground truth in `testdata/goca_arc_sample.afp`,
  regressions in `test_goca.py`. Skewed (non-orthogonal) arc matrices
  still approximate, as the column-norm axes assume no shear.)
- Unbracketed PTX fully handled (implicit page captures it, but no environment group).
- TLE (Tag Logical Element, `0xD3A090`) Find support is **coded and ready**
  (`_field_search_text` decodes its FQN X'02' name + X'36' value triplets,
  searchable under the Find "TLE tags" mode) but only validated against a
  synthetic field — no corpus file carries a TLE, so the real triplet
  layout/encoding is unconfirmed. Verify against a real sample when one
  turns up.

Done recently: a readable inspector **"Field data" column**
(`app._field_data_summary` + `foca.describe_foca_field`) decoding each
field to a one-line summary — PTX/NOP/TLE text, PGD/PTD/IDD geometry,
FND/FNC/FNP/FNO/FNI/FNM font metrics, CPD/CPC/CPI code pages, MCF/MIO/OBD/
OBP/IPD object maps, else a triplet rundown or hex; field-size column now
reports the full record (data + 9-byte header) like AFP inspectors; an
inspector **Find** feature (`_field_search_text` + `setupFind` in
`index.html`) — search PTX text, NOP text (the hidden comment/metadata
payloads), TLE tags, or field type, with case toggle, prev/next, in-place
match highlighting and multi-select field-count filtering;
STO text orientation (0/90/180/270°); FOCA raster-glyph
specimens; FOCA outline-font specimens — Type 1 PFB charstrings rasterized
to real glyph shapes (`type1.py`), with metadata-sheet fallback (PFB
detection, orientation dedup, FNN glyph names, fit-exempt grid); a red
"AFP resource" warning banner for page-less streams (font sets, code
pages, overlays…) with standalone-overlay rendering; a "missing resources"
panel listing font resources an MCF references but the file doesn't embed
(`app._missing_resources` / `triplets.mcf_resource_names`), matching what
other AFP viewers report and explaining substitute-font fallback; page
overlays (BMO/EMO + IPO); `__version__ = "1.0.0"` shown in the About modal with
the live Python version. Landing page bundles 6 sample cards (PTOCA /
GOCA / IOCA / FOCA / BCOCA / overlay), each generated by a
`tools/make_*_sample.py` script.
