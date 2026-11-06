"""Pages into one PDF: JPEG embedded as is, 1-bit PBM pages Flate-compressed.

Lossless: a page's turn (/Rotate) and skew (a transformation matrix) are drawing instructions;
the stored image is the scan, bit for bit. Standard library only.
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from typing import TYPE_CHECKING

from ._page import Page, encoded

if TYPE_CHECKING:
    from collections.abc import Sequence


class UnsupportedPage(ValueError):
    """Not a JPEG or binary PBM, or a damaged one."""


def jpeg_info(data: bytes) -> tuple[int, int, int]:
    """(width, height, components) from the first SOF marker."""
    if not data.startswith(b"\xff\xd8"):
        raise UnsupportedPage("no JPEG start marker")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise UnsupportedPage(f"bad marker at {i}")
        if data[i + 1] == 0xFF:  # fill bytes before a marker are allowed
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (seglen,) = struct.unpack_from(">H", data, i + 2)
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 10 > len(data):
                break
            height, width = struct.unpack_from(">HH", data, i + 5)
            return width, height, data[i + 9]
        i += 2 + seglen
    raise UnsupportedPage("no frame header")


_PBM_TOKEN = re.compile(rb"(?:\s|#[^\n]*\n?)*(\d+)")


def pbm_info(data: bytes) -> tuple[int, int, bytes]:
    """(width, height, rows) of a binary PBM (P4); comments in the header (GIMP writes one) and bytes
    after the last row (a trailing newline) are allowed."""
    if not data.startswith(b"P4"):
        raise UnsupportedPage("not a binary PBM")
    i, size = 2, []
    for _ in range(2):
        m = _PBM_TOKEN.match(data, i)
        if not m:
            raise UnsupportedPage("not a binary PBM")
        size.append(int(m.group(1)))
        i = m.end()
    if i >= len(data) or data[i : i + 1] not in (b" ", b"\t", b"\r", b"\n") or 0 in size:
        raise UnsupportedPage("not a binary PBM")
    w, h = size
    need = (w + 7) // 8 * h
    rows = data[i + 1 : i + 1 + need]
    if len(rows) != need:
        raise UnsupportedPage("PBM size does not match its header")
    return w, h, rows


def _image(data: bytes) -> tuple[int, int, bytes]:
    if data.startswith(b"P4"):
        w, h, rows = pbm_info(data)
        z = zlib.compress(rows, 9)
        # PBM: 1 = black; DeviceGray: 0 = black, hence the Decode array
        return (
            w,
            h,
            (
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                b"/BitsPerComponent 1 /Decode [1 0] /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream"
                % (w, h, len(z), z)
            ),
        )
    w, h, comps = jpeg_info(data)
    space = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}.get(comps)
    if space is None:
        raise UnsupportedPage(f"{comps} colour components")
    if comps == 4 and re.search(rb"\xff\xee..Adobe", data[:65536], re.DOTALL):
        # Adobe's CMYK JPEGs store the inks inverted (APP14 marker), as PDF readers expect it said
        space += b" /Decode [1 0 1 0 1 0 1 0]"
    return (
        w,
        h,
        (
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n%s\nendstream"
            % (w, h, space, len(data), data)
        ),
    )


def build(
    pages: Sequence[Page],
    dpi: int,
    rotations: Sequence[int] | None = None,
    skews: Sequence[float] | None = None,
) -> bytes:
    """rotations: per page 0/90/180/270, the clockwise turn a viewer applies (/Rotate).
    skews: per page degrees counter-clockwise to draw the image turned by, around its centre; the page
    grows to hold the whole turned scan, so nothing is cut off (a receipt near the edge, turned 14
    degrees, would otherwise move partly off the page).
    Both lossless: the image is stored as scanned."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below
    pages_obj = add(b"")
    kids = []
    for n, page in enumerate(pages):
        rotate = (rotations[n] if rotations else 0) % 360
        if rotate not in (0, 90, 180, 270):
            raise ValueError(f"rotation {rotate}")
        w, h, body = _image(encoded(page)[0])
        image = add(body)
        pw, ph = w * 72 / dpi, h * 72 / dpi
        mw, mh = pw, ph
        skew = skews[n] if skews else 0.0
        if skew:
            if not -45 < skew < 45:
                raise ValueError(f"skew {skew}")
            c, s = math.cos(math.radians(skew)), math.sin(math.radians(skew))
            mw, mh = pw * abs(c) + ph * abs(s), pw * abs(s) + ph * abs(c)  # the turned scan's bounding box
            # move the image centre to the origin, turn, move it to the centre of the (larger) page
            content = b"q 1 0 0 1 %.3f %.3f cm %.6f %.6f %.6f %.6f 0 0 cm %.3f 0 0 %.3f %.3f %.3f cm /Im0 Do Q" % (
                mw / 2,
                mh / 2,
                c,
                s,
                -s,
                c,
                pw,
                ph,
                -pw / 2,
                -ph / 2,
            )
        else:
            content = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pw, ph)
        stream = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Rotate %d "
                b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_obj, mw, mh, rotate, image, stream)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)
