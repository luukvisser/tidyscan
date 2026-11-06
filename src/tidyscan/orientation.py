"""Page orientation: a page scanned sideways or upside down is turned upright, losslessly.

Tesseract's orientation detection (OSD, --psm 0) answers "Rotate: N" (degrees
clockwise to turn the page upright) with a confidence. The PDF page gets
/Rotate N, so the image itself is never re-encoded.

Threshold: measured 01-11-2026 on two real pages (black-and-white and gray)
in all four orientations: 8 of 8 answers right; the real rotations had
confidence 5.8-6.9, an upright page as low as 1.9 (answer 0 anyway). OCRmyPDF's
default threshold (14) would have turned none of them. Default here: 3.
Second guard, from a 10-page corpus in gray and black-and-white (80 cases):
every wrong answer with confidence >= 3 came with a wrong script (Cyrillic
for an English book page that had become hard to read in black-and-white).
So a turn is only made when the detected script is one of ROTATE_SCRIPTS
(default Latin); otherwise the page is kept as scanned.
Needs the tesseract binary with its "osd" data; Pillow for the skew and 180-degree checks.
"""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
import shutil
import subprocess

from . import _budget
from ._page import Page, encoded
from ._page import workdir as _workdir

log = logging.getLogger(__name__)

TIMEOUT = 60


def available() -> bool:
    return shutil.which("tesseract") is not None


FLIP_MIN_WORDS = 10  # words of 3+ letters Tesseract is fairly sure of (confidence >= WORD_CONF)
FLIP_RATIO = 3.0  # ... and this many times more than the other way round
WORD_CONF = 60


def _turned_180(data: bytes) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("L")
    out = io.BytesIO()
    img.rotate(180).save(out, "PNG", dpi=img.info.get("dpi"))
    return out.getvalue()


def _word_score(data: bytes, workdir: str) -> int:
    """How many sure words of 3+ letters Tesseract reads on the page as it is."""
    path = os.path.join(workdir, f"osd-{secrets.token_hex(4)}.img")
    with open(path, "wb") as f:
        f.write(data)
    try:
        proc = subprocess.run(
            ["tesseract", path, "-", "--psm", "3", "tsv"], capture_output=True, timeout=_budget.timeout(TIMEOUT)
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    sure = 0
    for row in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) == 12 and cols[0] == "5":
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            word = cols[11]
            if conf >= WORD_CONF and sum(ch.isalpha() for ch in word) >= 3:
                sure += 1
    return sure


def quarter_by_words(page: Page, workdir: str | None = None, skew: float = 0.0) -> int | None:
    """90 or 270 when the page, straightened by skew, reads clearly better turned that way; None otherwise.

    For a page whose quarter turn is known but not its direction (90 and 270 level the same lines) and
    OSD is unsure: the same word count as for an unsure upside-down page. On the benchmark's illustrated
    book page turned 270 degrees (05-11-2026), with OSD below its threshold, 161-218 sure words the
    right way round against 8-56 the wrong way, in five of six cases; the sixth read none either way."""
    from PIL import Image

    workdir = _workdir(workdir)
    data, _ = encoded(page)
    img = Image.open(io.BytesIO(data)).convert("L")
    scores = {}
    for turn in (90, 270):
        im = img.rotate(-turn, expand=True)  # clockwise, as the PDF shows it
        if skew:
            im = im.rotate(skew, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=255)
        out = io.BytesIO()
        im.save(out, "PNG", dpi=img.info.get("dpi"))
        scores[turn] = _word_score(out.getvalue(), workdir)
    best, other = (90, 270) if scores[90] >= scores[270] else (270, 90)
    if scores[best] >= FLIP_MIN_WORDS and scores[best] >= FLIP_RATIO * max(scores[other], 1):
        log.info(
            "page turned %d degrees upright by the words read (%d that way, %d the other)",
            best,
            scores[best],
            scores[other],
        )
        return best
    log.info("90 or 270 not settled by the words read (%d at 90, %d at 270)", scores[90], scores[270])
    return None


def orientation(
    page: Page,
    workdir: str | None = None,
    min_confidence: float = 3.0,
    scripts: tuple[str, ...] = ("Latin",),
    skew: float = 0.0,
    quarter_turn: int = 0,
) -> int:
    """0, 90, 180 or 270: the clockwise turn that makes the page upright (0 when unsure).

    skew: the page's measured tilt (degrees counter-clockwise); OSD looks at the page straightened by it,
    as it cannot read a page tilted by more than a few degrees (a receipt at 30 degrees, 01-11-2026).
    quarter_turn: a clockwise turn applied before the skew, when the skew was measured in that frame (a
    page laid at more than 45 degrees); the answer is then relative to the turned page."""
    return detect(page, workdir, min_confidence, scripts, skew, quarter_turn)[0]


def detect(
    page: Page,
    workdir: str | None = None,
    min_confidence: float = 3.0,
    scripts: tuple[str, ...] = ("Latin",),
    skew: float = 0.0,
    quarter_turn: int = 0,
) -> tuple[int, bool]:
    """(turn, sure): orientation()'s answer, and whether OSD was sure of it. An upright answer is sure
    only with the confidence and script a turn needs; "kept as scanned" for doubt is not sure."""
    workdir = _workdir(workdir)
    data, ext = encoded(page)
    if skew or quarter_turn:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("L")
        dpi = img.info.get("dpi")
        if quarter_turn:
            img = img.rotate(-quarter_turn, expand=True)  # clockwise, as the PDF shows it
        if skew:
            img = img.rotate(skew, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=255)
        out = io.BytesIO()
        img.save(out, "PNG", dpi=dpi)
        data, ext = out.getvalue(), "png"
    path = os.path.join(workdir, f"osd-{secrets.token_hex(4)}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    try:
        proc = subprocess.run(
            ["tesseract", path, "-", "--psm", "0"], capture_output=True, timeout=_budget.timeout(TIMEOUT)
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("orientation detection failed (%s); page kept as scanned", e)
        return 0, False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = proc.stdout.decode("utf-8", "replace")
    rot = re.search(r"^Rotate:\s*(\d+)", out, re.M)
    conf = re.search(r"^Orientation confidence:\s*([\d.]+)", out, re.M)
    script = re.search(r"^Script:\s*(\S+)", out, re.M)
    if proc.returncode != 0 or not rot or not conf:
        # e.g. a page without text: "Too few characters"
        log.info("orientation unknown (little or no text); page kept as scanned")
        return 0, False
    angle, confidence = int(rot.group(1)) % 360, float(conf.group(1))
    if angle not in (0, 90, 180, 270):
        return 0, False
    script_ok = bool(script) and script.group(1) in scripts
    if angle == 180 and confidence < min_confidence and script and script.group(1) in scripts:
        # Upside down but unsure (a receipt at 2.9, 01-11-2026): read the page both ways round.
        upright, flipped = _word_score(data, workdir), _word_score(_turned_180(data), workdir)
        if flipped >= FLIP_MIN_WORDS and flipped >= FLIP_RATIO * max(upright, 1):
            log.info(
                "page turned 180 degrees upright (confidence %.1f; words read %d as turned, %d as scanned)",
                confidence,
                flipped,
                upright,
            )
            return 180, True
        log.info(
            "page may be turned 180 degrees, but confidence %.1f < %.1f and the words do not settle it "
            "(%d as turned, %d as scanned); kept as scanned",
            confidence,
            min_confidence,
            flipped,
            upright,
        )
        return 0, False
    if angle and confidence < min_confidence:
        log.info(
            "page may be turned %d degrees, but confidence %.1f < %.1f; kept as scanned",
            angle,
            confidence,
            min_confidence,
        )
        return 0, False
    if angle and not script_ok:
        log.info(
            "page may be turned %d degrees, but the script looks like %s; kept as scanned",
            angle,
            script.group(1) if script else "unknown",
        )
        return 0, False
    if angle:
        log.info("page turned %d degrees upright (confidence %.1f)", angle, confidence)
        return angle, True
    return 0, confidence >= min_confidence and script_ok
