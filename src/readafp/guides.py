"""Editorial guide content for SEO.

A small set of hand-written articles served at ``/guide/<slug>``. They form
a topic cluster around AFP / MO:DCA — answering the questions people search —
and link to each other and to the tool. Content is kept here as HTML so the
app needs no Markdown dependency; the surrounding chrome (head tags, header,
CTA, related links) lives in the templates.

Facts here mirror what the parser actually implements — no invented claims.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class Guide:
    """One article: routing slug, SEO metadata, and HTML body."""

    slug: str
    title: str          # used in <title> and <h1>
    description: str     # meta description + index blurb
    body: str            # HTML fragment
    related: List[str] = field(default_factory=list)  # other guide slugs


GUIDES: List[Guide] = [
    Guide(
        slug="what-is-an-afp-file",
        title="What Is an AFP File? Format, Uses, and How to Open One",
        description=(
            "An AFP (.afp) file is an IBM Advanced Function Presentation "
            "document used by mainframe and high-volume print systems. Learn "
            "what's inside one and how to open it."
        ),
        body="""
<p>An <strong>AFP file</strong> (extension <code>.afp</code>) holds a
print-ready document in IBM's <strong>Advanced Function Presentation</strong>
format. It is the output of mainframe and high-volume print systems used by
banks, insurers, utilities, and government agencies to produce statements,
invoices, bills, and customer letters — often millions of pages at a time.</p>

<h2>What is AFP used for?</h2>
<p>AFP was designed for <em>transactional</em> and <em>production</em>
printing: documents generated in bulk from data, printed on fast industrial
printers that understand the <strong>IPDS</strong> data stream. Its strengths
are precise, device-independent page layout and the ability to reuse shared
resources — fonts, electronic <em>overlays</em> (letterheads, forms), and
images — across an entire print run instead of embedding them in every page.</p>

<h2>What's inside an AFP file?</h2>
<p>Internally, an AFP file is a <a href="/guide/what-is-modca">MO:DCA</a> data
stream: a sequence of <em>structured fields</em> that describe each page's
content and exact positioning. The content itself is carried by several
object architectures:</p>
<ul>
  <li><strong>PTOCA</strong> — presentation text (the words on the page)</li>
  <li><strong>IOCA</strong> — raster images and photos</li>
  <li><strong>BCOCA</strong> — bar codes (such as QR Codes)</li>
  <li><strong>GOCA</strong> — vector graphics (lines, boxes, curves)</li>
  <li><strong>FOCA</strong> — fonts (raster and outline glyph data)</li>
</ul>
<p>Each is explained in <a href="/guide/afp-object-types">AFP object types
explained</a>.</p>

<h2>How do I open an AFP file?</h2>
<p>You can't usually double-click a <code>.afp</code> file — Windows and macOS
don't open AFP natively, and it isn't a PDF. You need an AFP viewer.
<a href="/">readAFP</a> opens any AFP file <strong>free, in your browser</strong>,
with no install: it shows the structured-field tree alongside a live render of
every page. See the step-by-step in
<a href="/guide/how-to-open-an-afp-file">how to open an AFP file online</a>,
or read how AFP compares to PDF in <a href="/guide/afp-vs-pdf">AFP vs PDF</a>.</p>
""",
        related=["how-to-open-an-afp-file", "afp-vs-pdf", "what-is-modca"],
    ),
    Guide(
        slug="how-to-open-an-afp-file",
        title="How to Open an AFP File Online (Free, No Install)",
        description=(
            "Can't open a .afp file? Here's how to view AFP documents free in "
            "your browser — no software to install — plus the other options."
        ),
        body="""
<p>Double-clicking an <a href="/guide/what-is-an-afp-file">AFP file</a>
(<code>.afp</code>) usually does nothing: it isn't a PDF, and neither Windows
nor macOS opens the format on its own. Here are your options, easiest first.</p>

<h2>Open it in your browser with readAFP</h2>
<p><a href="/">readAFP</a> is a free, browser-based AFP viewer and inspector —
nothing to install:</p>
<ol>
  <li>Go to <a href="/">readafp.com</a>.</li>
  <li>Click <strong>Choose File</strong> and pick your <code>.afp</code> file
      (or click a built-in sample to try it first).</li>
  <li>Switch between <strong>Inspect</strong> (the MO:DCA structured-field
      tree), <strong>Render</strong> (an SVG view of each page), and
      <strong>Split</strong>.</li>
</ol>
<p>It decodes text, images, bar codes, vector graphics, and fonts, and lets
you copy the page text or download it as <code>.txt</code>. Files up to 64&nbsp;MB
are supported.</p>

<h2>Other ways to open AFP</h2>
<ul>
  <li><strong>IBM AFP Workbench</strong> — a legacy Windows viewer; capable but
      dated and not always easy to obtain.</li>
  <li><strong>Commercial AFP viewers / transforms</strong> — several vendors
      sell AFP viewers and AFP-to-PDF converters for production use.</li>
  <li><strong>Convert to PDF</strong> — print pipelines often transform AFP to
      PDF for distribution; see <a href="/guide/afp-vs-pdf">AFP vs PDF</a>.</li>
</ul>

<p>For a quick look at what a file contains — its structure, text, and how each
page renders — opening it in <a href="/">readAFP</a> is the fastest route.</p>
""",
        related=["what-is-an-afp-file", "afp-vs-pdf", "afp-object-types"],
    ),
    Guide(
        slug="afp-vs-pdf",
        title="AFP vs PDF: What's the Difference?",
        description=(
            "AFP and PDF are both page formats, but they come from different "
            "worlds. Compare AFP (IBM, mainframe print) and PDF (Adobe, "
            "universal) and when each is used."
        ),
        body="""
<p>Both <strong>AFP</strong> and <strong>PDF</strong> describe the exact
appearance of a page, but they grew up solving different problems.
<a href="/guide/what-is-an-afp-file">AFP</a> comes from IBM's mainframe and
high-volume print world; PDF comes from Adobe and became the universal format
for sharing and viewing documents everywhere.</p>

<h2>Quick comparison</h2>
<table>
  <tr><th>&nbsp;</th><th>AFP</th><th>PDF</th></tr>
  <tr><td>Origin</td><td>IBM (mainframe print)</td><td>Adobe (universal)</td></tr>
  <tr><td>Primary use</td><td>High-volume transactional print (statements,
      invoices)</td><td>Viewing, sharing, archiving</td></tr>
  <tr><td>Native viewing</td><td>Needs an AFP viewer</td><td>Opens almost
      anywhere</td></tr>
  <tr><td>Resource reuse</td><td>Strong — shared fonts, overlays, images</td>
      <td>Usually embedded per file</td></tr>
  <tr><td>Print streaming</td><td>IPDS to production printers</td><td>Spooled /
      rasterized</td></tr>
</table>

<h2>Where AFP wins</h2>
<p>For producing millions of personalized pages, AFP's reusable resources and
record-level indexing are efficient, and IPDS streams pages straight to
industrial printers. Electronic <em>overlays</em> let one letterhead or form
back thousands of pages without bloating the file.</p>

<h2>Where PDF wins</h2>
<p>For anything a person needs to open, email, or archive, PDF is unbeatable —
every device and browser reads it, and PDF/A is a recognized archival standard.
Many print shops generate AFP for the press and a PDF copy for customers.</p>

<h2>Converting and viewing</h2>
<p>Production pipelines transform AFP to PDF for distribution. To simply look
inside an AFP file — its structure and how each page renders — open it in
<a href="/">readAFP</a>, which renders AFP to SVG in your browser. It's a viewer
and inspector, not a converter. New to the format? Start with
<a href="/guide/what-is-an-afp-file">what is an AFP file</a>.</p>
""",
        related=["what-is-an-afp-file", "how-to-open-an-afp-file", "what-is-modca"],
    ),
    Guide(
        slug="what-is-modca",
        title="What Is MO:DCA? (Mixed Object Document Content Architecture)",
        description=(
            "MO:DCA is the data-stream architecture behind AFP files. Learn how "
            "structured fields, the document hierarchy, and triplets work."
        ),
        body="""
<p><strong>MO:DCA</strong> — Mixed Object Document Content Architecture — is the
data-stream architecture that defines how an <a href="/guide/what-is-an-afp-file">
AFP file</a> is built. When people say "AFP," the bytes on disk are MO:DCA.</p>

<h2>Structured fields</h2>
<p>A MO:DCA document is a flat sequence of <strong>structured fields</strong>.
Each begins with a <code>0x5A</code> carriage-control byte, a length, and a
three-byte identifier (a type plus a category), followed by flags, a sequence
number, and the field's data. The identifier says what the field is — Begin
Page, Presentation Text, Include Object, and so on.</p>

<h2>The document hierarchy</h2>
<p>Structured fields nest into a predictable tree of <em>begin/end</em>
brackets:</p>
<ul>
  <li><strong>BDT … EDT</strong> — the document</li>
  <li><strong>BPG … EPG</strong> — a page</li>
  <li><strong>BAG … EAG</strong> — the active environment group (page geometry,
      fonts, color)</li>
  <li>object brackets for text, images, bar codes, and graphics</li>
</ul>

<h2>Triplets</h2>
<p>Many fields carry <strong>triplets</strong> — self-identifying
sub-parameters (a length, an id, and data) that add details like a fully
qualified resource name, a measurement unit, or a color. They tile a field's
data area exactly.</p>

<h2>See it for yourself</h2>
<p>The content inside MO:DCA comes from the
<a href="/guide/afp-object-types">object architectures</a> (PTOCA, IOCA, BCOCA,
GOCA, FOCA). <a href="/">readAFP</a> parses the MO:DCA stream and shows the
whole structured-field tree — every field, its decoded values, and its
triplets — beside a render of the page. Open an AFP file and expand the tree to
watch the hierarchy above in real data.</p>
""",
        related=["what-is-an-afp-file", "afp-object-types", "afp-vs-pdf"],
    ),
    Guide(
        slug="afp-object-types",
        title="AFP Object Types Explained: PTOCA, IOCA, BCOCA, GOCA, FOCA",
        description=(
            "AFP carries content in object architectures: PTOCA text, IOCA "
            "images, BCOCA bar codes, GOCA graphics, and FOCA fonts. Here's "
            "what each one does."
        ),
        body="""
<p>Inside an <a href="/guide/what-is-an-afp-file">AFP file</a>, the actual page
content is carried by a family of <em>object content architectures</em>, each
specialized for one kind of content. Here's what each one is — with a live
sample you can open in <a href="/">readAFP</a>.</p>

<h2>PTOCA — Presentation Text</h2>
<p>The text on the page. PTOCA control sequences position each run of
characters to an exact coordinate and set the font, color, and orientation.
This is how a statement's numbers and labels land precisely where the form
expects them. <a href="/inspect-sample/health_coverage">Open a text sample →</a></p>

<h2>IOCA — Image Object Content</h2>
<p>Raster images and photographs, including bilevel scans, grayscale, JPEG, and
banded CMYK color. <a href="/inspect-sample/ioca_image">Open an image sample →</a></p>

<h2>BCOCA — Bar Code Object Content</h2>
<p>Bar codes described by symbology, module size, and data, rather than as a
picture — so the printer renders a crisp symbol. QR Codes are common.
<a href="/inspect-sample/bcoca_qr">Open a bar-code sample →</a></p>

<h2>GOCA — Graphics Object Content</h2>
<p>Vector graphics: lines, boxes, arcs, and Bézier curves drawn from drawing
orders, plus area fills and color. <a href="/inspect-sample/goca_demo">Open a
graphics sample →</a></p>

<h2>FOCA — Font Object Content</h2>
<p>Fonts — both raster (bitmap) glyphs and outline font programs — that an AFP
file can embed so its text prints in exactly the right typeface.
<a href="/inspect-sample/foca_font">Open a font sample →</a></p>

<p>All of these sit within the <a href="/guide/what-is-modca">MO:DCA</a>
structured-field stream. To see them decoded together, open any file in
<a href="/">readAFP</a>.</p>
""",
        related=["what-is-an-afp-file", "what-is-modca", "how-to-open-an-afp-file"],
    ),
]

GUIDES_BY_SLUG = {g.slug: g for g in GUIDES}
