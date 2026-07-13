"""IM Image Object (IM/1) decoder.

The IM image object is MO:DCA's legacy raster image, kept for migration.
Unlike IOCA (BIM…EIM), it is a *bilevel* raster carried by a small set of
fixed structured fields:

    BII (X'D3A87B')  Begin IM Image Object   — names the object
    IOC (X'D3A77B')  IM Image Output Control — scaling/placement (ignored)
    IID (X'D3A67B')  IM Image Input Descriptor — resolution + image size
    ICP (X'D3AC7B')  IM Image Cell Position  — one per image cell
    IRD (X'D3EE7B')  IM Image Raster Data    — the cell's 1-bit-per-pel bits
    EII (X'D3A97B')  End IM Image Object

An image is either *simple* (one IRD covering the whole image, no ICP) or
*celled*: a grid of rectangular cells, each an (ICP, IRD) pair placed at the
cell's XCOset/YCOset offset. A cell can be replicated to fill a larger fill
rectangle (XFilSize/YFilSize). We reassemble every cell into one bitmap the
size of the IID's XSize×YSize, then pack it as a 1-bpp PNG.

A pel bit of 1 is toned (foreground / black), matching IOCA's convention, so
we invert to PNG's 0 = black — the same path IOCA bilevel uses.

Reference: MO:DCA Reference, AFPC-0004-10, "IM Image Object" (Appendix C).
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .ioca import pack_png

logger = logging.getLogger(__name__)

# Guard against a pathological IID declaring a huge image (image points are
# 16-bit, so at most 32767×32767, but the packed bitmap would be ~134 MB).
_MAX_IM_PIXELS = 8_000_000

_FFFF = 0xFFFF


def _u16(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 2], "big")


@dataclass
class ImImage:
    """A decoded IM image: a bilevel PNG plus its geometry."""

    width: int       # XSize, in image points (pels)
    height: int      # YSize, in image points
    resolution: int  # image points per inch (XUnits / 10)
    png: bytes       # packed 1-bpp grayscale PNG (0 = black)


def parse_ioc_origin(ioc: bytes) -> Tuple[int, int]:
    """Return the object-area origin (XoaOset, YoaOset) from an IOC field.

    IOC layout: XoaOset(3) YoaOset(3) XoaOrent(2) YoaOrent(2) … — the origin
    is given in image points (the IID resolution's units). An IM image placed
    directly on a page carries its position here; one included through a page
    segment (IPS) usually leaves it X'000000' and is positioned by the IPS.
    """
    if len(ioc) < 6:
        return 0, 0
    return (
        int.from_bytes(ioc[0:3], "big"),
        int.from_bytes(ioc[3:6], "big"),
    )


def _parse_iid(iid: bytes) -> Optional[Tuple[int, int, int, int, int]]:
    """Return (xsize, ysize, resolution, xcsized, ycsized) from an IID.

    Fixed layout (image points, big-endian u16):
        14-15 XUnits   (10 × resolution)   18-19 XSize   20-21 YSize
        28-29 XCSizeD  (default cell width) 30-31 YCSizeD (default cell height)
    """
    if len(iid) < 32:
        return None
    xunits = _u16(iid, 14)
    xsize = _u16(iid, 18)
    ysize = _u16(iid, 20)
    xcsized = _u16(iid, 28)
    ycsized = _u16(iid, 30)
    resolution = xunits // 10
    return xsize, ysize, resolution, xcsized, ycsized


def decode_im_image(
    iid: bytes, cells: List[Tuple[Optional[bytes], bytes]]
) -> Optional[ImImage]:
    """Reassemble an IM image object into a bilevel PNG.

    ``cells`` is the sequence of (ICP-data-or-None, IRD-data) pairs captured
    between BII and EII, in order. A simple (non-celled) image passes a single
    (None, raster) pair covering the whole XSize×YSize grid.
    """
    parsed = _parse_iid(iid)
    if parsed is None:
        return None
    xsize, ysize, resolution, xcsized, ycsized = parsed
    if xsize <= 0 or ysize <= 0:
        return None
    if xsize * ysize > _MAX_IM_PIXELS:
        logger.info("skipping oversized IM image: %d×%d pels", xsize, ysize)
        return None

    row_bytes = (xsize + 7) // 8
    bitmap = bytearray(row_bytes * ysize)  # 0 = background (white after invert)

    for icp, ird in cells:
        if icp is not None and len(icp) >= 8:
            xco, yco = _u16(icp, 0), _u16(icp, 2)
            xcs, ycs = _u16(icp, 4), _u16(icp, 6)
            if xcs == _FFFF:
                xcs = xcsized
            if ycs == _FFFF:
                ycs = ycsized
            xfil = _u16(icp, 8) if len(icp) >= 10 else xcs
            yfil = _u16(icp, 10) if len(icp) >= 12 else ycs
            if xfil == _FFFF:
                xfil = xcs
            if yfil == _FFFF:
                yfil = ycs
        else:  # simple image: one cell covering the whole grid
            xco = yco = 0
            xcs, ycs = xsize, ysize
            xfil, yfil = xsize, ysize
        if xcs <= 0 or ycs <= 0:
            continue

        cell_row_bytes = (xcs + 7) // 8
        # Paint the fill rectangle by replicating the cell (truncated to the
        # image bounds). Most images fill exactly one cell (xfil==xcs).
        for fy in range(yfil):
            dy = yco + fy
            if dy >= ysize:
                break
            sy = fy % ycs
            src_row = sy * cell_row_bytes
            dst_row = dy * row_bytes
            for fx in range(xfil):
                dx = xco + fx
                if dx >= xsize:
                    break
                sx = fx % xcs
                src_byte = src_row + (sx >> 3)
                if src_byte >= len(ird):
                    continue
                if (ird[src_byte] >> (7 - (sx & 7))) & 1:
                    bitmap[dst_row + (dx >> 3)] |= 0x80 >> (dx & 7)

    # IM pel 1 = toned (black); PNG grayscale 0 = black — invert.
    inverted = bytes(b ^ 0xFF for b in bitmap)
    png = pack_png(xsize, ysize, 1, 0, row_bytes, inverted)
    return ImImage(width=xsize, height=ysize, resolution=resolution, png=png)
