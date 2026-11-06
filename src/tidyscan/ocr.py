"""A searchable text layer on a PDF, by OCRmyPDF (the ocrmypdf command).

Two engines read the page images OCRmyPDF renders:
  paddle     PaddleOCR PP-OCRv5 on OpenVINO (tidyscan.paddle, via the OCRmyPDF plugin
             tidyscan.ocrmypdf_paddle); Latin script, needs the [paddle] extra and the models
  tesseract  OCRmyPDF's own, with the Tesseract language data for `languages`
engine="tesseract" is the default: on the benchmark of 05-11-2026 (45 pages, 188 variants) it found
90% of the words against Paddle's 83%, better on letters, documents, forms and book pages, in about
half the time and a sixth of the memory. Paddle reads receipts, photo captions and tables better;
"paddle" asks for it, "auto" takes it when it is installed and covers the languages, else Tesseract.
When Paddle fails on a document, Tesseract reads that document instead (logged), so a scan never
loses its text layer to the newer engine. `languages` only steers Tesseract.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess

from . import paddle
from ._page import workdir as _workdir

TIMEOUT = 600
ENGINES = ("auto", "paddle", "tesseract")
PLUGIN = "tidyscan.ocrmypdf_paddle"
ENTRY_POINT_GROUP = "ocrmypdf"  # OCRmyPDF 16 and 17 load these plugins by themselves

log = logging.getLogger(__name__)


class OcrError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("ocrmypdf") is not None


def choose(engine: str, languages: str) -> str:
    """The engine that will read: "paddle" or "tesseract"."""
    if engine not in ENGINES:
        raise ValueError(f"engine {engine!r}: one of {', '.join(ENGINES)}")
    if engine == "tesseract":
        return "tesseract"
    lacking = paddle.missing()
    why = (
        "not installed (missing " + ", ".join(lacking) + ")"
        if lacking
        else ""
        if paddle.covers(languages)
        else f"not trained for all of {languages}"
    )
    if why:
        (log.warning if engine == "paddle" else log.debug)("PaddleOCR %s; Tesseract reads instead", why)
        return "tesseract"
    return "paddle"


def add_text_layer(
    pdf: bytes,
    languages: str = "eng",
    workdir: str | None = None,
    timeout: float = TIMEOUT,
    engine: str = "tesseract",
    pdfa: bool = False,
) -> bytes:
    """The PDF with a text layer under each page image; languages: Tesseract codes joined by +.

    Each engine run gets `timeout` seconds, so a Paddle run that fails late and the Tesseract run
    after it can take twice that. Raises OcrError (the caller keeps the PDF without text).

    pdfa: PDF/A-2 instead of a plain PDF (OCRmyPDF's --output-type pdfa-2). Measured 06-11-2026 on
    OCRmyPDF 16.7: still lossless, as the plain output: a 1-bit page is stored as CCITT G4 instead of
    Flate, rendered pixel for pixel the same; a JPEG keeps its bytes (one extra end marker)."""
    output = "pdfa-2" if pdfa else "pdf"
    selectable = _ocrmypdf_major() >= 17
    if choose(engine, languages) == "paddle":
        # one worker: each would load its own models; OpenVINO uses the CPUs within a page
        extra = ["--jobs", "1"]
        if not plugin_installed():  # loaded twice, OCRmyPDF refuses to start
            extra = ["--plugin", PLUGIN, *extra]
        if selectable:
            extra += ["--ocr-engine", "paddle"]
        try:
            return _ocrmypdf(pdf, languages, workdir, timeout, extra, output)
        except OcrError as e:
            log.warning("PaddleOCR failed (%s); Tesseract reads this document instead", e)
    if selectable:
        # on 17 the plugin also answers --ocr-engine auto when an installed package loads it
        return _ocrmypdf(pdf, languages, workdir, timeout, ["--ocr-engine", "tesseract"], output)
    if plugin_installed():
        log.warning("OCRmyPDF 16 with the plugin installed as a package reads with PaddleOCR only")
    return _ocrmypdf(pdf, languages, workdir, timeout, [], output)


def plugin_installed() -> bool:
    """Whether an installed package (an entry point in OCRmyPDF's plugin group) has OCRmyPDF load the plugin itself."""
    from importlib.metadata import entry_points

    return any(ep.value == PLUGIN for ep in entry_points(group=ENTRY_POINT_GROUP))


def _ocrmypdf_major() -> int:
    """OCRmyPDF's major version in this Python, 0 when it is not installed as a package."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return int(version("ocrmypdf").split(".")[0])
    except (PackageNotFoundError, ValueError):
        return 0


def _ocrmypdf(
    pdf: bytes, languages: str, workdir: str | None, timeout: float, extra: list[str], output: str = "pdf"
) -> bytes:
    stem = os.path.join(_workdir(workdir), f"ocr-{secrets.token_hex(4)}")
    src, dst = stem + "-in.pdf", stem + "-out.pdf"
    try:
        with open(src, "wb") as f:
            f.write(pdf)
        proc = subprocess.run(
            ["ocrmypdf", "--quiet", "--output-type", output, "-l", languages, *extra, src, dst],
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise OcrError(f"exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-300:].strip()}")
        with open(dst, "rb") as f:
            return f.read()
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrError(str(e)) from e
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass
