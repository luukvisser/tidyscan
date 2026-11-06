"""Skew (a page laid crooked on the glass): measured from the text lines, corrected in the PDF.

Measuring: Tesseract's layout analysis finds the text lines and reports each
line's baseline slope (hOCR). The angle is the median over the lines of at
least MIN_LINE_MM and MIN_LINE_SHARE of the longest line, each weighted by
its length squared (a short line's baseline is less precise). Photos and
drawings have no text lines and so do not count.

Tesseract finds no lines on a page that is too crooked (from about 8
degrees in the corpus), and on a steep one it cuts the lines into flat
fragments that measure near 0 and survive the check below (a receipt at 26
degrees came out at -7, 01-11-2026). So a coarse estimate comes first,
independent of Tesseract: the projection profile (turn a small copy of the
page in steps of COARSE_STEP up to COARSE_MAX and keep the angle whose rows
are most uneven, text rows against the white between them). Tesseract then
looks at the page as scanned and turned by that estimate. Every estimate is
checked: the page is turned by it and measured once more; the answer is the
estimate plus what is left, and only when that remainder is within
MAX_RESIDUAL, the lines still agree, and the answer is within AGREE of the
coarse estimate. Otherwise: unsure, and the page is kept as scanned.
A remainder above MAX_RESIDUAL but below AGREE gets one more look, turned by
the estimate plus that remainder: an upside-down receipt at -27.63 degrees
had +0.52 left and was kept as scanned (benchmark 05-11-2026, weak spot 3).
Lines that measure within SIDEWAYS of +-90 degrees end the search: the page
lies sideways, and the other candidates and the 300-dpi pass would only cost
time (pages turned a quarter took a median of 21 s, up to 130, against 7-8
upright; weak spot 2); straighten() then measures a quarter turn on.

Correcting: the PDF draws the untouched scan turned by the angle (a
transformation matrix in the page's content stream), on a page large enough
for the whole turned scan, so the stored image is bit for bit the scan.

Measured 01-11-2026 (tests/eval_corpus.py: 10 real pages from OCRmyPDF's test
resources and Rijksoverheid, gray and black-and-white, tilts up to 14
degrees, and receipt-sized copies near the edge of an A4 scan) and on a real
receipt laid at 14.3 degrees. Dropped on the way: a projection method
(confidently wrong by 8.75 degrees on a page with a photo), Leptonica's
pixFindSkew and the PyPI package deskew (less accurate), and a single look
without the check (wrong by 0.99 degrees at 14).

Needs the tesseract binary and Pillow (the straighten extra).
"""

from __future__ import annotations

import io
import itertools
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
from typing import TYPE_CHECKING

from . import _budget
from ._page import Page, encoded
from ._page import workdir as _workdir

if TYPE_CHECKING:
    from PIL import Image
else:
    try:
        from PIL import Image
    except ImportError:  # Pillow is optional: available() is then False
        Image = None

log = logging.getLogger(__name__)

PASSES = (150, 300)  # work resolutions: 150 is fast; 300 only when it finds too few lines (small print)
COARSE_DPI = 75  # projection profile resolution: text rows of small print still apart
COARSE_STEP = 1.0  # degrees between the coarse angles, then refined to COARSE_STEP / 4
COARSE_MAX = 45.0  # a page tilted further is a page turned a quarter and tilted the other way
COARSE_PEAK = 1.5  # the best angle's score over the median score; below: no clear text rows
COARSE_PEAKS = 3  # peaks offered to Tesseract, strongest first (the strongest may be the scan's streaks)
COARSE_SEPARATION = 5.0  # degrees between two peaks that count as different
AGREE = 3.0  # degrees between the coarse estimate and Tesseract's answer
MIN_LINES = 3
MIN_LINE_MM = 10  # shorter lines have an imprecise baseline
MIN_LINE_SHARE = 0.3  # ... and so do lines below this share of the longest (relative to the document)
MAX_SPREAD = 0.5  # degrees, weighted median absolute deviation of the line angles
MAX_RESIDUAL = 0.5  # degrees left after turning by the estimate; more: the estimate is not trusted
MAX_ANGLE = COARSE_MAX
SIDEWAYS = 5.0  # degrees from +-90: text lines that run up the page; the page lies sideways
TIMEOUT = 120
LINE_RE = re.compile(
    r"class=['\"]ocr_(?:line|textfloat|header|caption)['\"][^>]*?title=['\"]bbox (\d+) \d+ (\d+) \d+;"
    r" baseline (-?[\d.]+) -?[\d.]+"
)


def available() -> bool:
    return shutil.which("tesseract") is not None and Image is not None


def _weighted_median(values: list[tuple[float, float]]) -> float:
    """values: (value, weight); the value where half the weight lies on either side."""
    values = sorted(values)
    half, run = sum(w for _, w in values) / 2, 0.0
    for v, w in values:
        run += w
        if run >= half:
            return v
    return values[-1][0]


def _base(page: Page, dpi: int, quarter_turn: int, work_dpi: int) -> Image.Image:
    data, ext = encoded(page)
    img = Image.open(io.BytesIO(data))
    scale = min(1.0, work_dpi / dpi)
    target = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))  # from the original size
    if ext == "jpg":
        img.draft("L", target)  # fast JPEG decode at reduced size (never below target)
    img = img.convert("L")
    if img.size != target:
        img = img.resize(target, Image.Resampling.BOX)
    if quarter_turn:
        img = img.rotate(-quarter_turn, expand=True)  # clockwise, as the PDF will show it
    return img


def _measure(img: Image.Image, work_dpi: int, workdir: str) -> tuple[float, float, int] | None:
    """(angle, spread, lines) of the text on img, or None with too few usable lines."""
    path = os.path.join(workdir, f"skew-{secrets.token_hex(4)}.png")
    img.save(path, dpi=(work_dpi, work_dpi))
    try:
        proc = subprocess.run(
            ["tesseract", path, "-", "--psm", "3", "--dpi", str(work_dpi), "hocr"],
            capture_output=True,
            timeout=_budget.timeout(TIMEOUT),
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        return None
    found = [
        (math.degrees(math.atan(float(slope))), (int(x1) - int(x0)) / work_dpi * 25.4)
        for x0, x1, slope in LINE_RE.findall(proc.stdout.decode("utf-8", "replace"))
    ]
    longest = max((mm for _, mm in found), default=0)
    lines = [(a, mm**2) for a, mm in found if mm >= MIN_LINE_MM and mm >= longest * MIN_LINE_SHARE]
    if len(lines) < MIN_LINES:
        return None
    median = _weighted_median(lines)
    return median, _weighted_median([(abs(a - median), w) for a, w in lines]), len(lines)


def _turned(img: Image.Image, angle: float) -> Image.Image:
    return img.rotate(angle, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=255) if angle else img


def _profile_score(img: Image.Image, angle: float) -> float:
    """How uneven the rows of img turned by angle are: high when text rows are level."""
    turned = _turned(img, angle)
    rows = turned.resize((1, turned.height), Image.Resampling.BOX).tobytes()  # each row's mean brightness
    return sum((a - b) ** 2 for a, b in itertools.pairwise(rows))


def coarse_peaks(page: Page, dpi: int, quarter_turn: int = 0, limit: int = COARSE_PEAKS) -> list[float]:
    """The projection profile's clear peaks (degrees counter-clockwise), strongest first, refined.

    More than one, because the strongest is not always the text: on a black-and-white scan of a
    receipt laid at -41 degrees (01-11-2026) the scan's own straight streaks peaked at 0 and +-89, at
    five times the receipt's rows. Peaks closer than COARSE_SEPARATION to a stronger one are dropped;
    each must stand out by COARSE_PEAK from the median.
    """
    img = _base(page, dpi, quarter_turn, COARSE_DPI)
    steps = int(COARSE_MAX / COARSE_STEP)
    angles = [a * COARSE_STEP for a in range(-steps, steps + 1)]
    scores = {a: _profile_score(img, a) for a in angles}
    ordered = sorted(scores.values())
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return []
    local = [
        a
        for i, a in enumerate(angles)
        if scores[a] >= scores[angles[max(i - 1, 0)]]
        and scores[a] >= scores[angles[min(i + 1, len(angles) - 1)]]
        and scores[a] / median >= COARSE_PEAK
    ]
    chosen: list[float] = []
    for a in sorted(local, key=scores.__getitem__, reverse=True):
        if all(abs(a - c) >= COARSE_SEPARATION for c in chosen):
            chosen.append(a)
        if len(chosen) == limit:
            break
    refined = []
    for best in chosen:
        fine = {best + k * COARSE_STEP / 4: 0.0 for k in range(-3, 4)}
        for f in fine:
            fine[f] = _profile_score(img, f)
        refined.append(max(fine, key=fine.__getitem__))
    return refined


def coarse(page: Page, dpi: int, quarter_turn: int = 0) -> float | None:
    """The strongest projection-profile angle (degrees counter-clockwise), or None without clear rows."""
    peaks = coarse_peaks(page, dpi, quarter_turn, limit=1)
    return peaks[0] if peaks else None


def detect(page: Page, dpi: int, quarter_turn: int = 0, workdir: str | None = None) -> float | None:
    """Degrees counter-clockwise that make the text lines level, or None when unsure.

    quarter_turn: the clockwise turn (0/90/180/270) the page will be shown with; the angle is
    measured as seen that way. workdir: where Tesseract's input is written for a moment."""
    if not available():
        return None
    workdir = _workdir(workdir)
    peaks = coarse_peaks(page, dpi, quarter_turn)
    candidates = [0.0] + [p for p in peaks if abs(p) > AGREE]
    why = "no text lines found"
    for work_dpi in sorted({min(p, dpi) for p in PASSES}):
        base = _base(page, dpi, quarter_turn, work_dpi)
        for pre in candidates:
            first = _measure(_turned(base, pre), work_dpi, workdir)
            if first is None:
                continue
            angle, spread, n = first
            if spread > MAX_SPREAD:
                why = f"text lines disagree ({n} lines, spread {spread:.2f} degrees)"
                continue
            estimate = pre + angle
            if abs(abs(estimate) - 90) <= SIDEWAYS:
                log.info(
                    "skew unsure (text lines at %.2f degrees: the page lies sideways); page kept as scanned", estimate
                )
                return None
            check = _measure(_turned(base, estimate), work_dpi, workdir)
            if check is not None and check[1] <= MAX_SPREAD and MAX_RESIDUAL < abs(check[0]) < AGREE:
                again = _measure(_turned(base, estimate + check[0]), work_dpi, workdir)
                if again is not None and again[1] <= MAX_SPREAD and abs(again[0]) <= MAX_RESIDUAL:
                    estimate, check = estimate + check[0], again
            if check is None or check[1] > MAX_SPREAD or abs(check[0]) > MAX_RESIDUAL:
                why = (
                    f"estimate {estimate:.2f} degrees not confirmed ("
                    + ("no lines after turning" if check is None else f"{check[0]:+.2f} left")
                    + ")"
                )
                continue
            result = estimate + check[0]
            if abs(abs(result) - 90) <= SIDEWAYS:
                log.info(
                    "skew unsure (text lines at %.2f degrees: the page lies sideways); page kept as scanned", result
                )
                return None
            if peaks and all(abs(result - p) > AGREE for p in peaks):
                why = (
                    f"text lines at {result:.2f} degrees, but the page's rows at {', '.join(f'{p:.2f}' for p in peaks)}"
                )
                continue
            if abs(result) > MAX_ANGLE:
                why = f"text lines at {result:.1f} degrees: too crooked to be a skew"
                continue
            return round(result, 2)
    log.info("skew unsure (%s); page kept as scanned", why)
    return None
