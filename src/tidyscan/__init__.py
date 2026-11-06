"""Auto-rotate and deskew scanned pages into a lossless PDF, with an optional OCR text layer.

    turn, angle = tidyscan.straighten(page, 300)
    data = tidyscan.pdf.build([page], 300, [turn], [angle])
    data = tidyscan.ocr.add_text_layer(data, "eng+nld")   # Tesseract; engine="paddle" for receipts

A page is JPEG bytes, binary PBM (P4) bytes, or an object with to_pbm().
"""

from __future__ import annotations

import logging

from . import _budget, hocr, models, ocr, orientation, paddle, pdf, skew
from ._page import Page, PbmSource, encoded
from .ocr import OcrError
from .pdf import UnsupportedPage

__version__ = "0.3.0"
__all__ = [
    "OcrError",
    "Page",
    "PbmSource",
    "UnsupportedPage",
    "hocr",
    "models",
    "ocr",
    "orientation",
    "paddle",
    "pdf",
    "skew",
    "straighten",
]

log = logging.getLogger(__name__)

STEEP_EDGE = 2.0  # degrees from +-45: a steep-path answer there is as likely the other quarter turn
BUDGET = 300.0  # seconds of Tesseract per page at most (see _budget)
MAX_PIXELS = 200_000_000  # above Pillow's own refusal (2 x 89.5 M pixels); an A4 page at 1200 dpi is 140 M


def straighten(
    page: Page,
    dpi: int,
    *,
    turn: int = 0,
    rotate: bool = True,
    deskew: bool = True,
    min_confidence: float = 3.0,
    scripts: tuple[str, ...] = ("Latin",),
    deskew_min: float = 0.3,
    workdir: str | None = None,
    budget: float | None = BUDGET,
) -> tuple[int, float]:
    """(clockwise turn 0/90/180/270, skew in degrees counter-clockwise) for pdf.build; 0 for what is off,
    unsure or unavailable.

    turn: a turn already known (from the scanner); it is kept and not detected. deskew_min: smaller
    skews are not visible and left alone. budget: seconds of Tesseract for this page at most (None: no
    limit); what is not measured by then is kept as scanned. A page larger than MAX_PIXELS is not read
    at all. Never raises: straightening is a nicety and must not cost a page (a JPEG that Pillow
    cannot decode is still a page the PDF can carry)."""
    if _too_large(page):
        return turn, 0.0
    token = _budget.start(budget)
    try:
        return _straighten(page, dpi, turn, rotate, deskew, min_confidence, scripts, deskew_min, workdir)
    finally:
        _budget.end(token)


def _too_large(page: Page) -> bool:
    try:
        data, ext = encoded(page)
        w, h = pdf.pbm_info(data)[:2] if ext == "pbm" else pdf.jpeg_info(data)[:2]
    except Exception:  # noqa: BLE001  (the PDF writer reports a damaged page)
        return False
    if w * h > MAX_PIXELS:
        log.warning("page of %d x %d pixels: too large to read; kept as scanned", w, h)
        return True
    return False


def _straighten(
    page: Page,
    dpi: int,
    turn: int,
    rotate: bool,
    deskew: bool,
    min_confidence: float,
    scripts: tuple[str, ...],
    deskew_min: float,
    workdir: str | None,
) -> tuple[int, float]:
    # Skew first, then the orientation of the straightened page: OSD cannot read a page tilted by
    # more than a few degrees. A 180-degree turn leaves the text lines' angle as it is; a quarter
    # turn means the lines were measured across the text, so the skew is measured again upright.
    known = bool(turn)
    found = _detect(page, dpi, turn, workdir) if deskew else None
    base = 0
    if found is None and deskew and rotate and not known:
        # A page laid at more than 45 degrees (a receipt at about 60, upside down, 01-11-2026): the skew
        # search covers +-45, so its text rows only show a quarter turn on. Measured there, the skew is
        # an ordinary one, and OSD then only has to tell 90 from 270.
        steep = _detect(page, dpi, 90, workdir)
        if steep is not None and abs(abs(steep) - 45) < STEEP_EDGE:
            # a chart with slanted axis labels came out a quarter plus about 45 degrees (benchmark 05-11-2026)
            log.info("skew unsure (%.2f degrees a quarter turn on: too near 45); page kept as scanned", steep)
        elif steep is not None:
            base, found = 90, steep
            log.info("page lies at more than 45 degrees; measured a quarter turn on")
    angle = found if found is not None and abs(found) >= deskew_min else 0.0
    if not known and rotate:
        try:
            more, sure = orientation.detect(page, workdir, min_confidence, scripts, skew=angle, quarter_turn=base)
        except Exception as e:  # noqa: BLE001
            log.warning("orientation not determined (%s); page kept as scanned", e)
            more, sure = 0, False
        if base and not sure:
            # The steep path's quarter turn is only half an answer: 90 and 270 level the same lines, and
            # only OSD on the turned page tells them apart. Unsure, it was kept, and every wrong turn in the
            # benchmark (10, 05-11-2026) came from here. Rotations commute, so the angle measured in the
            # turned frame straightens the page as scanned too: sideways, but level. First the words
            # decide, as for an unsure upside-down page.
            try:
                by_words = orientation.quarter_by_words(page, workdir, skew=angle)
            except Exception as e:  # noqa: BLE001
                log.warning("words not read (%s)", e)
                by_words = None
            if by_words:
                base, more = by_words, 0
            else:
                log.info("quarter turn not confirmed by the orientation check; page kept sideways")
                base = 0
        turn = (base + more) % 360
        if more in (90, 270):
            angle = _skew(page, dpi, turn, deskew, deskew_min, workdir)
    if angle:
        log.info("page straightened by %.2f degrees", angle)
    return turn, angle


def _detect(page: Page, dpi: int, turn: int, workdir: str | None) -> float | None:
    """skew.detect, None when unsure or failing."""
    try:
        return skew.detect(page, dpi, turn, workdir)
    except Exception as e:  # noqa: BLE001
        log.warning("skew not measured (%s); page kept as scanned", e)
        return None


def _skew(page: Page, dpi: int, turn: int, deskew: bool, deskew_min: float, workdir: str | None) -> float:
    if not deskew:
        return 0.0
    found = _detect(page, dpi, turn, workdir)
    return found if found is not None and abs(found) >= deskew_min else 0.0
