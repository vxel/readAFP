"""GOCA (Graphics Object Content Architecture) drawing-order decoder.

GAD (Graphics Data) fields carry drawing order streams.  Each stream
consists of one or more Begin Segment commands (code 0x70) followed by
the drawing orders that make up that segment.  Drawing orders come in
four formats:

  Fixed 1-byte   code only                  GNOP1 (0x00)
  Fixed 2-byte   code + one operand byte     GSCOL (0x0A), GBAR (0x68), …
  Long format    code + length + data        GSCP (0x21), GLINE (0xC1), …
  Extended       0xFE + qualifier + u16len   GLGD (0xFEDC), GRGD (0xFEDD)

Format rule (from AFPC-0008-03 §7):
  - code 0x00              → fixed 1-byte
  - code 0xFE              → extended
  - code 0x71              → fixed 2-byte (architecture note)
  - upper nibble < 8 and lower nibble ≥ 8  → fixed 2-byte
  - otherwise              → long format

Coordinate system: GPS Y axis points upward; SVG Y axis points downward.
All GPS coordinates are converted to SVG space as:
    svg_x = gps_x − xlwind
    svg_y = ytwind − gps_y
where xlwind/ytwind are the GPS window origin returned by parse_gdd().

Reference: GOCA Reference, AFPC-0008-03 (docs/specs/afp-goca-reference-03.pdf).
"""

import logging
import struct
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple
from xml.sax.saxutils import quoteattr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard OCA / GOCA 2-byte color table (same values as PTOCA STC_COLORS).
# GSCOL maps a 1-byte index to a prefixed 2-byte value 0xFF00 | index.
# GSECOL uses the 2-byte value directly.
# ---------------------------------------------------------------------------
_OCA_COLORS = {
    0xFF01: "#0000ff",  # blue
    0xFF02: "#ff0000",  # red
    0xFF03: "#ff00ff",  # pink / magenta
    0xFF04: "#00ff00",  # green
    0xFF05: "#00ffff",  # cyan / turquoise
    0xFF06: "#ffff00",  # yellow
    0xFF08: "#000000",  # black
    0xFF10: "#a52a2a",  # brown
    0xFF07: "#000000",  # device default
    0xFF00: "#000000",  # drawing default
}
_DEFAULT_COLOR = "#000000"

# GSLT line-type code → SVG stroke-dasharray (multiples of stroke-width).
_DASH_ARRAYS = {
    0x00: "",       # drawing default → solid
    0x01: "1 3",    # dotted
    0x02: "6 3",    # short-dashed
    0x03: "6 3 1 3",    # dash-dot
    0x04: "1 1",    # double-dotted
    0x05: "12 3",   # long-dashed
    0x06: "6 3 1 1 1 3",  # dash-double-dot
    0x07: "",       # solid
    0x08: None,     # invisible: suppress stroke entirely
}

# GSLE line-end → SVG stroke-linecap
_LINE_CAPS = {0x00: "round", 0x01: "butt", 0x02: "square", 0x03: "round"}

# GSLJ line-join → SVG stroke-linejoin
_LINE_JOINS = {0x00: "round", 0x01: "bevel", 0x02: "round", 0x03: "miter"}


# ---------------------------------------------------------------------------
# Order-format classifier
# ---------------------------------------------------------------------------

def _order_format(code: int) -> str:
    if code == 0x00:
        return "fixed1"
    if code == 0xFE:
        return "extended"
    if code == 0x71:  # End Segment — fixed 2-byte by architecture note
        return "fixed2"
    if (code >> 4) < 8 and (code & 0x0F) >= 8:
        return "fixed2"
    return "long"


# ---------------------------------------------------------------------------
# Drawing-order iterator
# ---------------------------------------------------------------------------

def iter_orders(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """Yield ``(order_code, params)`` from concatenated GAD data bytes.

    The GAD byte stream contains one or more Begin Segment commands
    (0x70) each followed by *SEGL* bytes of drawing orders.  Unchained
    segments (FLAG2 bit 0 / MSB = 1) are skipped per the AFP GOCA spec.
    Extended-format orders (0xFE) yield an int code (0xFExx).
    """
    pos = 0
    n = len(data)
    while pos < n:
        code = data[pos]
        if code != 0x70:
            logger.debug("skipping unexpected byte 0x%02X at GAD offset %d", code, pos)
            pos += 1
            continue
        # Begin Segment: code(1) + param_length(1=12) + params(12)
        # params: NAME(4) FLAG1(1) FLAG2(1) SEGL(2) P/SNAME(4)
        if pos + 14 > n:
            break
        flag2 = data[pos + 7]
        unchained = bool(flag2 & 0x80)  # bit 0 (MSB) set → unchained → ignore
        segl = struct.unpack_from(">H", data, pos + 8)[0]
        pos += 14
        seg_end = min(pos + segl, n)
        if unchained:
            pos = seg_end
            continue

        while pos < seg_end:
            oc = data[pos]
            fmt = _order_format(oc)
            if fmt == "fixed1":
                yield oc, b""
                pos += 1
            elif fmt == "fixed2":
                if pos + 2 > seg_end:
                    pos = seg_end
                    break
                yield oc, bytes(data[pos + 1 : pos + 2])
                pos += 2
            elif fmt == "long":
                if pos + 2 > seg_end:
                    pos = seg_end
                    break
                length = data[pos + 1]
                end = pos + 2 + length
                if end > seg_end:
                    pos = seg_end
                    break
                yield oc, bytes(data[pos + 2 : end])
                pos = end
            else:  # extended 0xFE
                if pos + 4 > seg_end:
                    pos = seg_end
                    break
                qualifier = data[pos + 1]
                ext_len = struct.unpack_from(">H", data, pos + 2)[0]
                ext_end = pos + 4 + ext_len
                if ext_end > seg_end:
                    pos = seg_end
                    break
                yield (0xFE00 | qualifier), bytes(data[pos + 4 : ext_end])
                pos = ext_end


# ---------------------------------------------------------------------------
# GDD (Graphics Data Descriptor) parser
# ---------------------------------------------------------------------------

def parse_gdd(data: bytes) -> Optional[Tuple[float, int, int, int, int]]:
    """Parse GDD field data → ``(gps_upi, xlwind, xrwind, ybwind, ytwind)``.

    The GDD body is a sequence of self-identifying parameters
    (code u8 + length u8 + data).  We look for the mandatory Window
    Specification (0xF6) which gives the GPS coordinate system.

    Returns ``None`` if no Window Specification is found.
    """
    pos = 0
    while pos + 2 <= len(data):
        code = data[pos]
        length = data[pos + 1]
        end = pos + 2 + length
        if end > len(data):
            break
        if code == 0xF6 and length >= 18:
            p = data[pos + 2 :]  # parameter bytes (after code + length)
            # p[0]: FLAGS, p[1]: RES3, p[2]: CFORMAT
            ubase = p[3]   # 0 = ten inches, 1 = ten centimetres
            xresol = struct.unpack_from(">H", p, 4)[0]
            # GPS units per inch
            if ubase == 0:
                gps_upi = xresol / 10.0
            else:
                gps_upi = xresol * 0.254  # units/10cm → units/inch
            xlwind = struct.unpack_from(">h", p, 10)[0]
            xrwind = struct.unpack_from(">h", p, 12)[0]
            ybwind = struct.unpack_from(">h", p, 14)[0]
            ytwind = struct.unpack_from(">h", p, 16)[0]
            return gps_upi, xlwind, xrwind, ybwind, ytwind
        pos = end
    return None


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _gscol_to_css(operand: int) -> str:
    """GSCOL 1-byte palette index → CSS colour."""
    return _OCA_COLORS.get(0xFF00 | operand, _DEFAULT_COLOR)


def _gsecol_to_css(two_bytes: int) -> str:
    """GSECOL / STC 2-byte colour index → CSS colour."""
    return _OCA_COLORS.get(two_bytes, _DEFAULT_COLOR)


def _gspcol_to_css(params: bytes) -> str:
    """GSPCOL process colour → CSS colour (RGB and CMYK only)."""
    # params: reserved(1) color_space(1) nBitsR(1) nBitsG(1) nBitsB(1)
    #          nBitsA(1) color_value(...)
    if len(params) < 6:
        return _DEFAULT_COLOR
    color_space = params[1]
    if color_space == 0x01 and len(params) >= 9:  # RGB
        r, g, b = params[6], params[7], params[8]
        return f"#{r:02x}{g:02x}{b:02x}"
    if color_space == 0x04 and len(params) >= 10:  # CMYK → RGB
        c, m, y, k = params[6] / 255, params[7] / 255, params[8] / 255, params[9] / 255
        r = int(255 * (1 - c) * (1 - k))
        g = int(255 * (1 - m) * (1 - k))
        b = int(255 * (1 - y) * (1 - k))
        return f"#{r:02x}{g:02x}{b:02x}"
    return _DEFAULT_COLOR


# ---------------------------------------------------------------------------
# Drawing state
# ---------------------------------------------------------------------------

@dataclass
class _State:
    cx: float = 0.0       # current GPS x
    cy: float = 0.0       # current GPS y
    color: str = _DEFAULT_COLOR
    stroke_width: float = 1.0   # GPS units (from GSLW / GSFLW)
    line_type: int = 7          # 7 = solid
    line_cap: str = "round"
    line_join: str = "round"
    pattern_set: int = 0
    pattern_sym: int = 0x10     # 0x10 = solid fill in default set
    in_area: bool = False
    area_boundary: bool = True
    area_path: List[str] = field(default_factory=list)
    # GSAP defaults per spec: P=Q=1, R=S=0 (unit circle).
    arc_p: float = 1.0
    arc_q: float = 1.0
    arc_r: float = 0.0
    arc_s: float = 0.0


def _stroke_attrs(st: "GocaContext") -> str:
    """Build SVG stroke attribute string from drawing state."""
    s = st.state
    dash = _DASH_ARRAYS.get(s.line_type, "")
    if dash is None:  # invisible
        return 'stroke="none"'
    parts = [
        f'stroke={quoteattr(s.color)}',
        f'stroke-width="{s.stroke_width:.3g}"',
        f'stroke-linecap="{s.line_cap}"',
        f'stroke-linejoin="{s.line_join}"',
    ]
    if dash:
        sw = s.stroke_width or 1
        # dasharray values are in GPS units scaled by stroke-width
        parts.append(f'stroke-dasharray="{" ".join(str(round(float(v)*sw, 3)) for v in dash.split())}"')
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Rendering context
# ---------------------------------------------------------------------------

@dataclass
class GocaContext:
    """Mutable context for a GOCA drawing session."""

    xlwind: int = 0
    ytwind: int = 0
    state: _State = field(default_factory=_State)
    out: List[str] = field(default_factory=list)


def _sx(ctx: GocaContext, gx: float) -> float:
    """GPS x → SVG x."""
    return gx - ctx.xlwind


def _sy(ctx: GocaContext, gy: float) -> float:
    """GPS y → SVG y (Y-axis flip)."""
    return ctx.ytwind - gy


def _coord(ctx: GocaContext, gx: float, gy: float) -> str:
    return f"{_sx(ctx, gx):.3g},{_sy(ctx, gy):.3g}"


def _fill_style(st: _State) -> str:
    """CSS fill for the current area state."""
    if st.pattern_sym in (0x00, 0x0F, 0x40):
        return "none"
    if st.pattern_sym == 0x10:
        return st.color
    # Built-in hatch patterns: approximate as semi-transparent fill
    if 0x01 <= st.pattern_sym <= 0x08:
        return st.color  # TODO: proper hatching
    return "none"


# ---------------------------------------------------------------------------
# Per-order handlers
# ---------------------------------------------------------------------------

def _handle_gscp(ctx: GocaContext, params: bytes) -> None:
    if len(params) >= 4:
        ctx.state.cx = struct.unpack_from(">h", params, 0)[0]
        ctx.state.cy = struct.unpack_from(">h", params, 2)[0]


def _handle_gscol(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.color = _gscol_to_css(params[0])


def _handle_gsecol(ctx: GocaContext, params: bytes) -> None:
    if len(params) >= 2:
        ctx.state.color = _gsecol_to_css(
            struct.unpack_from(">H", params)[0]
        )


def _handle_gspcol(ctx: GocaContext, params: bytes) -> None:
    ctx.state.color = _gspcol_to_css(params)


def _handle_gslw(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.stroke_width = max(params[0], 1)


def _handle_gsflw(ctx: GocaContext, params: bytes) -> None:
    if len(params) >= 2:
        multiplier = params[0] + params[1] / 256.0
        if multiplier > 0:
            ctx.state.stroke_width = multiplier


def _handle_gslt(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.line_type = params[0]


def _handle_gsle(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.line_cap = _LINE_CAPS.get(params[0], "round")


def _handle_gslj(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.line_join = _LINE_JOINS.get(params[0], "round")


def _handle_gsps(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.pattern_set = params[0]


def _handle_gspt(ctx: GocaContext, params: bytes) -> None:
    if params:
        ctx.state.pattern_sym = params[0] or 0x10


def _handle_gsap(ctx: GocaContext, params: bytes) -> None:
    if len(params) >= 8:
        ctx.state.arc_p = struct.unpack_from(">h", params, 0)[0]
        ctx.state.arc_q = struct.unpack_from(">h", params, 2)[0]
        ctx.state.arc_r = struct.unpack_from(">h", params, 4)[0]
        ctx.state.arc_s = struct.unpack_from(">h", params, 6)[0]


def _emit_polyline(ctx: GocaContext, points_svg: List[str]) -> None:
    """Emit a <polyline> or add to area path."""
    if not points_svg:
        return
    if ctx.state.in_area:
        if not ctx.state.area_path:
            ctx.state.area_path.append(f"M {points_svg[0]}")
        else:
            ctx.state.area_path.append(f"L {points_svg[0]}")
        for pt in points_svg[1:]:
            ctx.state.area_path.append(f"L {pt}")
        return
    pts = " ".join(points_svg)
    attrs = _stroke_attrs(ctx)
    ctx.out.append(f'<polyline points="{pts}" fill="none" {attrs}/>')


def _s16_pair(data: bytes, off: int) -> Tuple[int, int]:
    return (
        struct.unpack_from(">h", data, off)[0],
        struct.unpack_from(">h", data, off + 2)[0],
    )


def _handle_gline(ctx: GocaContext, params: bytes) -> None:
    """GLINE / GCLINE — polyline at given or current position."""
    # GLINE (0xC1): first pair is start, remaining are endpoints
    # GCLINE (0x81): current position is start, pairs are endpoints
    # We detect which by how the caller passes the start.
    # This function handles the common body (list of points after start):
    # params_line is the pair sequence starting from the first endpoint.
    if len(params) < 4:
        return
    pts = []
    for i in range(0, len(params) - 3, 4):
        gx, gy = _s16_pair(params, i)
        pts.append(_coord(ctx, gx, gy))
    if not pts:
        return
    ctx.state.cx, ctx.state.cy = _s16_pair(params, len(params) - 4)
    _emit_polyline(ctx, pts)


def _handle_gline_at(ctx: GocaContext, params: bytes) -> None:
    """GLINE: first pair = start position, rest = endpoints."""
    if len(params) < 4:
        return
    gx0, gy0 = _s16_pair(params, 0)
    ctx.state.cx, ctx.state.cy = gx0, gy0
    pts = [_coord(ctx, gx0, gy0)]
    for i in range(4, len(params) - 3, 4):
        gx, gy = _s16_pair(params, i)
        pts.append(_coord(ctx, gx, gy))
        ctx.state.cx, ctx.state.cy = gx, gy
    _emit_polyline(ctx, pts)


def _handle_gcline_at(ctx: GocaContext, params: bytes) -> None:
    """GCLINE: current position is start, params are endpoints."""
    pts = [_coord(ctx, ctx.state.cx, ctx.state.cy)]
    for i in range(0, len(params) - 3, 4):
        gx, gy = _s16_pair(params, i)
        pts.append(_coord(ctx, gx, gy))
        ctx.state.cx, ctx.state.cy = gx, gy
    _emit_polyline(ctx, pts)


def _s8(b: int) -> int:
    return b if b < 128 else b - 256


def _handle_grline_at(ctx: GocaContext, params: bytes) -> None:
    """GRLINE: given start, then relative (signed 8-bit) offsets."""
    if len(params) < 4:
        return
    gx0, gy0 = _s16_pair(params, 0)
    ctx.state.cx, ctx.state.cy = gx0, gy0
    pts = [_coord(ctx, gx0, gy0)]
    for i in range(4, len(params) - 1, 2):
        ctx.state.cx += _s8(params[i])
        ctx.state.cy += _s8(params[i + 1])
        pts.append(_coord(ctx, ctx.state.cx, ctx.state.cy))
    _emit_polyline(ctx, pts)


def _handle_gcrline_at(ctx: GocaContext, params: bytes) -> None:
    """GCRLINE: current position is start, relative offsets follow."""
    pts = [_coord(ctx, ctx.state.cx, ctx.state.cy)]
    for i in range(0, len(params) - 1, 2):
        ctx.state.cx += _s8(params[i])
        ctx.state.cy += _s8(params[i + 1])
        pts.append(_coord(ctx, ctx.state.cx, ctx.state.cy))
    _emit_polyline(ctx, pts)


def _handle_gbox(ctx: GocaContext, params: bytes, at_given: bool) -> None:
    """GBOX / GCBOX — rectangle."""
    if at_given:
        if len(params) < 10:
            return
        gx0, gy0 = _s16_pair(params, 2)
        gx1, gy1 = _s16_pair(params, 6)
    else:
        if len(params) < 6:
            return
        gx0, gy0 = ctx.state.cx, ctx.state.cy
        gx1, gy1 = _s16_pair(params, 2)
    ctx.state.cx, ctx.state.cy = gx0, gy0
    # SVG rect: top-left corner + positive width/height
    sx0, sy0 = _sx(ctx, min(gx0, gx1)), _sy(ctx, max(gy0, gy1))
    w = abs(gx1 - gx0)
    h = abs(gy1 - gy0)
    if ctx.state.in_area:
        # Box as closed path
        ctx.state.area_path.append(
            f"M {_sx(ctx, gx0):.3g},{_sy(ctx, gy0):.3g} "
            f"L {_sx(ctx, gx1):.3g},{_sy(ctx, gy0):.3g} "
            f"L {_sx(ctx, gx1):.3g},{_sy(ctx, gy1):.3g} "
            f"L {_sx(ctx, gx0):.3g},{_sy(ctx, gy1):.3g} Z"
        )
        return
    st = ctx.state
    fill = _fill_style(st)
    stroke_part = _stroke_attrs(ctx) if st.line_type != 0x08 else 'stroke="none"'
    ctx.out.append(
        f'<rect x="{sx0:.3g}" y="{sy0:.3g}" width="{w:.3g}" height="{h:.3g}" '
        f'fill={quoteattr(fill)} {stroke_part}/>'
    )


def _arc_to_ellipse(ctx: GocaContext, cx: float, cy: float, multiplier: float) -> str:
    """Render a GFARC/GCFARC as SVG <ellipse> (or <circle>)."""
    st = ctx.state
    # GSAP transform (spec): X' = P·X + R·Y, Y' = S·X + Q·Y — the matrix
    # [[P, R], [S, Q]]. Its columns (P, S) and (R, Q) are the images of the
    # unit x/y vectors: the ellipse's semi-axes for an orthogonal transform.
    import math
    p, q, r, s = st.arc_p * multiplier, st.arc_q * multiplier, \
                  st.arc_r * multiplier, st.arc_s * multiplier

    # Semi-axis lengths in GPS units (column norms)
    rx_gps = math.sqrt(p * p + s * s)
    ry_gps = math.sqrt(r * r + q * q)
    if rx_gps < 1e-6 or ry_gps < 1e-6:
        return ""

    # Rotation angle of the x-axis image (then reflect for SVG Y-flip)
    angle_rad = math.atan2(s, p)  # GPS x-axis direction
    angle_svg = -math.degrees(angle_rad)  # negate because Y is flipped in SVG

    scx = _sx(ctx, cx)
    scy = _sy(ctx, cy)

    fill = _fill_style(st)
    stroke_part = _stroke_attrs(ctx)

    if abs(rx_gps - ry_gps) < 0.01 and abs(angle_svg) < 0.5:
        return (
            f'<circle cx="{scx:.3g}" cy="{scy:.3g}" r="{rx_gps:.3g}" '
            f'fill={quoteattr(fill)} {stroke_part}/>'
        )
    if abs(angle_svg) < 0.5:
        return (
            f'<ellipse cx="{scx:.3g}" cy="{scy:.3g}" '
            f'rx="{rx_gps:.3g}" ry="{ry_gps:.3g}" '
            f'fill={quoteattr(fill)} {stroke_part}/>'
        )
    return (
        f'<ellipse cx="{scx:.3g}" cy="{scy:.3g}" '
        f'rx="{rx_gps:.3g}" ry="{ry_gps:.3g}" '
        f'fill={quoteattr(fill)} {stroke_part} '
        f'transform="rotate({angle_svg:.3g} {scx:.3g} {scy:.3g})"/>'
    )


def _handle_gfarc(ctx: GocaContext, params: bytes, at_given: bool) -> None:
    """GFARC / GCFARC — full circle or ellipse."""
    if at_given:
        if len(params) < 6:
            return
        gx = struct.unpack_from(">h", params, 0)[0]
        gy = struct.unpack_from(">h", params, 2)[0]
        mh, mfr = params[4], params[5]
    else:
        if len(params) < 2:
            return
        gx, gy = ctx.state.cx, ctx.state.cy
        mh, mfr = params[0], params[1]
    ctx.state.cx, ctx.state.cy = gx, gy
    multiplier = mh + mfr / 256.0
    if multiplier < 1e-6:
        return
    if ctx.state.in_area:
        # Add full-arc to area path as a closed sub-path
        import math
        st = ctx.state
        rx = math.sqrt(st.arc_p ** 2 + st.arc_s ** 2) * multiplier
        ry = math.sqrt(st.arc_r ** 2 + st.arc_q ** 2) * multiplier
        scx = _sx(ctx, gx)
        scy = _sy(ctx, gy)
        # Two half-arcs to form a closed ellipse in SVG
        ctx.state.area_path.append(
            f"M {scx - rx:.3g},{scy:.3g} "
            f"A {rx:.3g},{ry:.3g} 0 1 0 {scx + rx:.3g},{scy:.3g} "
            f"A {rx:.3g},{ry:.3g} 0 1 0 {scx - rx:.3g},{scy:.3g} Z"
        )
        return
    markup = _arc_to_ellipse(ctx, gx, gy, multiplier)
    if markup:
        ctx.out.append(markup)


def _handle_gparc(ctx: GocaContext, params: bytes, at_given: bool) -> None:
    """GPARC / GCPARC — partial arc (SVG arc path)."""
    import math
    if at_given:
        if len(params) < 18:
            return
        gx0, gy0 = _s16_pair(params, 0)    # line start
        gcx, gcy = _s16_pair(params, 4)    # arc centre
        mh, mfr = params[8], params[9]
        start_raw = struct.unpack_from(">I", params, 10)[0]  # unsigned 32-bit
        sweep_raw = struct.unpack_from(">I", params, 14)[0]
        off = 0
    else:
        if len(params) < 14:
            return
        gx0, gy0 = ctx.state.cx, ctx.state.cy
        gcx, gcy = _s16_pair(params, 0)
        mh, mfr = params[4], params[5]
        start_raw = struct.unpack_from(">I", params, 6)[0]
        sweep_raw = struct.unpack_from(">I", params, 10)[0]
        off = 0

    multiplier = mh + mfr / 256.0
    if multiplier < 1e-6:
        return

    st = ctx.state
    # GSAP transform (spec): X' = P·X + R·Y, Y' = S·X + Q·Y — the matrix
    # M = [[p, r], [s, q]] scaled by the multiplier. Its columns (p, s) and
    # (r, q) are the images of the unit x/y vectors: the semi-axes for an
    # orthogonal transform — matching the full-arc path.
    p, q = st.arc_p * multiplier, st.arc_q * multiplier
    r, s = st.arc_r * multiplier, st.arc_s * multiplier
    rx = math.sqrt(p * p + s * s)
    ry = math.sqrt(r * r + q * q)
    if rx < 1e-6 or ry < 1e-6:
        return

    start_deg = (start_raw / 65536.0) % 360.0
    sweep_deg = (sweep_raw / 65536.0) % 360.0
    if sweep_deg < 1e-6:
        return

    # x-axis rotation of the ellipse, negated for the SVG Y-flip.
    angle_svg = -math.degrees(math.atan2(s, p)) or 0.0  # avoid "-0"

    # Arc endpoints: a unit-circle parameter angle a maps to the GPS point
    # centre + M·(cos a, sin a); _sx/_sy then apply the Y-flip to screen.
    def _endpoint(a_deg: float):
        a = math.radians(a_deg)
        ca, sa = math.cos(a), math.sin(a)
        return gcx + p * ca + r * sa, gcy + s * ca + q * sa

    gx_start, gy_start = _endpoint(start_deg)
    gx_end, gy_end = _endpoint(start_deg + sweep_deg)
    sx_start, sy_start = _sx(ctx, gx_start), _sy(ctx, gy_start)
    sx_end, sy_end = _sx(ctx, gx_end), _sy(ctx, gy_end)

    # Line from (gx0, gy0) to the arc start.
    sx0, sy0 = _sx(ctx, gx0), _sy(ctx, gy0)
    large_arc = 1 if abs(sweep_deg) > 180 else 0
    # Determinant P·Q − R·S > 0 sweeps CCW (spec p. 24); the Y-flip turns
    # that into SVG flag 0. A reflecting matrix reverses it once more.
    sweep_flag = 0 if (p * q - r * s) >= 0 else 1

    d = (
        f"M {sx0:.3g},{sy0:.3g} "
        f"L {sx_start:.3g},{sy_start:.3g} "
        f"A {rx:.3g},{ry:.3g} {angle_svg:.3g} {large_arc} {sweep_flag} "
        f"{sx_end:.3g},{sy_end:.3g}"
    )
    # Spec: "The current position is moved to the endpoint of the arc."
    ctx.state.cx, ctx.state.cy = gx_end, gy_end

    attrs = _stroke_attrs(ctx)
    ctx.out.append(f'<path d={quoteattr(d)} fill="none" {attrs}/>')


def _handle_gcbez(ctx: GocaContext, params: bytes, at_given: bool) -> None:
    """GCBEZ / GCCBEZ — cubic Bézier curve(s)."""
    if at_given:
        # params: start(x0,y0) + sets of 3 points (cp1, cp2, end)
        if len(params) < 16:
            return
        gx0, gy0 = _s16_pair(params, 0)
        pairs = [(struct.unpack_from(">h", params, i)[0],
                  struct.unpack_from(">h", params, i + 2)[0])
                 for i in range(4, len(params) - 3, 4)]
        base_off = 4
    else:
        # params: sets of 3 points (cp1, cp2, end)
        if len(params) < 12:
            return
        gx0, gy0 = ctx.state.cx, ctx.state.cy
        pairs = [(struct.unpack_from(">h", params, i)[0],
                  struct.unpack_from(">h", params, i + 2)[0])
                 for i in range(0, len(params) - 3, 4)]
        base_off = 0

    if len(pairs) < 3:
        return

    ctx.state.cx, ctx.state.cy = gx0, gy0
    d_parts = [f"M {_coord(ctx, gx0, gy0)}"]
    i = 0
    while i + 2 < len(pairs):
        cp1 = pairs[i]
        cp2 = pairs[i + 1]
        end = pairs[i + 2]
        d_parts.append(
            f"C {_coord(ctx, *cp1)} {_coord(ctx, *cp2)} {_coord(ctx, *end)}"
        )
        ctx.state.cx, ctx.state.cy = end
        i += 3

    d = " ".join(d_parts)
    attrs = _stroke_attrs(ctx)
    ctx.out.append(f'<path d={quoteattr(d)} fill="none" {attrs}/>')


def _handle_gbar(ctx: GocaContext, params: bytes) -> None:
    """GBAR — begin area definition."""
    st = ctx.state
    st.in_area = True
    st.area_boundary = bool(params and (params[0] & 0x40))  # BOUNDARY bit 1
    st.area_path = []


def _handle_gear(ctx: GocaContext, params: bytes) -> None:
    """GEAR — end area, emit filled shape."""
    st = ctx.state
    if not st.in_area:
        return
    st.in_area = False
    if not st.area_path:
        return

    d = " ".join(st.area_path) + " Z"
    fill = _fill_style(st)
    if st.area_boundary:
        stroke_part = _stroke_attrs(ctx)
    else:
        stroke_part = 'stroke="none"'
    ctx.out.append(
        f'<path d={quoteattr(d)} fill={quoteattr(fill)} {stroke_part}/>'
    )
    st.area_path = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class GocaGraphic:
    """A GOCA graphic rendered to SVG.  Coordinates are in GPS units."""

    svg: str        # SVG <g> fragment with GPS-unit coordinates (Y flipped)
    gps_w: int      # GPS window width
    gps_h: int      # GPS window height


def draw_goca(gdd_data: bytes, gad_data: bytes) -> Optional[GocaGraphic]:
    """Decode a GOCA object → SVG fragment.

    ``gdd_data`` is the raw bytes of the GDD structured field.
    ``gad_data`` is the concatenated bytes of all GAD structured fields.

    Returns ``None`` if the GDD has no Window Specification or the GAD
    is empty.
    """
    gdd = parse_gdd(gdd_data)
    if gdd is None:
        logger.debug("GOCA: no Window Specification in GDD, skipping")
        return None
    gps_upi, xlwind, xrwind, ybwind, ytwind = gdd
    gps_w = xrwind - xlwind
    gps_h = ytwind - ybwind
    if gps_w <= 0 or gps_h <= 0:
        logger.debug("GOCA: degenerate GPS window (%d×%d)", gps_w, gps_h)
        return None

    ctx = GocaContext(xlwind=xlwind, ytwind=ytwind)

    for code, params in iter_orders(gad_data):
        try:
            _dispatch(ctx, code, params)
        except Exception:
            logger.debug("GOCA: error in order 0x%02X", code, exc_info=True)

    svg = "".join(ctx.out)
    if not svg:
        return None
    return GocaGraphic(svg=svg, gps_w=gps_w, gps_h=gps_h)


def _dispatch(ctx: GocaContext, code: int, params: bytes) -> None:  # noqa: C901
    """Route a single drawing order to its handler."""
    if code in (0x00, 0x71):  # GNOP1, End Segment
        return
    if code == 0x01:          # GCOMT
        return
    if code == 0x04:          # GSGCH Segment Characteristics
        return
    if code == 0x08:          # GSPS Set Pattern Set
        _handle_gsps(ctx, params)
    elif code == 0x0A:        # GSCOL Set Color
        _handle_gscol(ctx, params)
    elif code == 0x0C:        # GSMX Set Mix (no-op for viewer)
        pass
    elif code == 0x0D:        # GSBMX Set Background Mix (no-op)
        pass
    elif code == 0x11:        # GSFLW Set Fractional Line Width
        _handle_gsflw(ctx, params)
    elif code == 0x18:        # GSLT Set Line Type
        _handle_gslt(ctx, params)
    elif code == 0x19:        # GSLW Set Line Width
        _handle_gslw(ctx, params)
    elif code == 0x1A:        # GSLE Set Line End
        _handle_gsle(ctx, params)
    elif code == 0x1B:        # GSLJ Set Line Join
        _handle_gslj(ctx, params)
    elif code == 0x20:        # GSCLT Set Custom Line Type (no-op)
        pass
    elif code == 0x21:        # GSCP Set Current Position
        _handle_gscp(ctx, params)
    elif code == 0x22:        # GSAP Set Arc Parameters
        _handle_gsap(ctx, params)
    elif code == 0x26:        # GSECOL Set Extended Color
        _handle_gsecol(ctx, params)
    elif code == 0x28:        # GSPT Set Pattern Symbol
        _handle_gspt(ctx, params)
    elif code in (0x29, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C):  # marker/char attrs
        pass
    elif code in (0x33, 0x34, 0x35):   # Set Character Cell/Angle/Shear (no-op)
        pass
    elif code in (0x3E, 0x5E):         # GEPROL, GECP (structural)
        pass
    elif code == 0x43:                 # GSPIK (no-op)
        pass
    elif code == 0x60:                 # GEAR End Area
        _handle_gear(ctx, params)
    elif code == 0x68:                 # GBAR Begin Area
        _handle_gbar(ctx, params)
    elif code == 0x80:                 # GCBOX Box at Current Position
        _handle_gbox(ctx, params, at_given=False)
    elif code == 0x81:                 # GCLINE Line at Current Position
        _handle_gcline_at(ctx, params)
    elif code in (0x82, 0xC2):        # GCMRK, GMRK (no-op for now)
        pass
    elif code in (0x83, 0xC3):        # GCCHST, GCHST (char string, skip)
        pass
    elif code == 0x85:                 # GCFLT Fillet at Current Position (approx)
        _handle_gcline_at(ctx, params)
    elif code == 0x87:                 # GCFARC Full Arc at Current Position
        _handle_gfarc(ctx, params, at_given=False)
    elif code in (0x91, 0xD1, 0x92, 0x93):  # GCBIMG/GBIMG/GIMD/GEIMG (skip)
        pass
    elif code == 0xA0:                 # GSPRP (no-op)
        pass
    elif code == 0xA1:                 # GCRLINE Relative Line at Current Position
        _handle_gcrline_at(ctx, params)
    elif code == 0xA3:                 # GCPARC Partial Arc at Current Position
        _handle_gparc(ctx, params, at_given=False)
    elif code == 0xA5:                 # GCCBEZ Cubic Bezier at Current Position
        _handle_gcbez(ctx, params, at_given=False)
    elif code == 0xB2:                 # GSPCOL Set Process Color
        _handle_gspcol(ctx, params)
    elif code == 0xC0:                 # GBOX Box at Given Position
        _handle_gbox(ctx, params, at_given=True)
    elif code == 0xC1:                 # GLINE Line at Given Position
        _handle_gline_at(ctx, params)
    elif code == 0xC5:                 # GFLT Fillet at Given Position (approx)
        _handle_gline_at(ctx, params)
    elif code == 0xC7:                 # GFARC Full Arc at Given Position
        _handle_gfarc(ctx, params, at_given=True)
    elif code in (0xDE, 0xDF):        # GBCP, GDPT (custom patterns, skip)
        pass
    elif code == 0xE1:                 # GRLINE Relative Line at Given Position
        _handle_grline_at(ctx, params)
    elif code == 0xE3:                 # GPARC Partial Arc at Given Position
        _handle_gparc(ctx, params, at_given=True)
    elif code == 0xE5:                 # GCBEZ Cubic Bezier at Given Position
        _handle_gcbez(ctx, params, at_given=True)
    elif code in (0xFEDC, 0xFEDD):   # GLGD, GRGD (gradients, skip)
        pass
    else:
        logger.debug("GOCA: unhandled order 0x%02X (%d param bytes)", code, len(params))
