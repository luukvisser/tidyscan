"""`tidyscan watch IN OUT`: document folders (as scankeyd writes them) into PDFs, one by one.

A document folder holds the pages as scanned (page-0001.jpg, page-0002.pbm, ...) and a
document.json with at least "pages" and "resolution" (scankeyd also writes "function", "color",
"scanned"). scankeyd puts each in IN/<function>/<name>/ and renames it there only when complete,
so a folder that has its document.json is ready; folders whose name starts with a dot are
skipped (scankeyd's .part, and .failed below).

For each document, oldest first: every page straightened (turned upright and levelled, when
Tesseract is there and it is not switched off), one PDF of them, a text layer when its function
is one of ocr_folders and OCRmyPDF is there, written to OUT/<name>.pdf (OUT/<function>/<name>.pdf
with keep_folders) as a hidden .part file and renamed. Only then the folder goes. A document that
cannot be read moves to IN/.failed/; when OCR fails, the PDF is written without text (logged).
A text layer goes on a document whose document.json says "ocr": true (scankeyd: FUNC_OCR=bw,ocr);
for a document without that field, on the function folders in ocr_folders.
Without Tesseract this is the plain "pages to PDF" step: the PDF writer needs the standard
library only.

With a status file (--status), a monitor such as Home Assistant can see how it goes (Status).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass

from . import ocr, orientation, pdf, skew, straighten

log = logging.getLogger(__name__)

PAGE_RE = re.compile(r"^page-\d{4}\.(jpg|pbm)$")
FUNCTION_RE = re.compile(r"^[a-z0-9_-]{1,32}$")  # a function as a folder name in OUT (keep_folders)
FAILED = ".failed"
STUCK = ".tidyscan-stuck.json"  # in IN: converted folders that could not be removed, never converted again
WAITING_AFTER = 600  # seconds: a ready document older than this counts as waiting (status)
HEARTBEAT = 60  # seconds between status writes when nothing happens


@dataclass
class Options:
    ocr_languages: str = "eng"
    ocr_folders: tuple[str, ...] = ("ocr",)  # the function folders whose documents get a text layer; "*": all
    engine: str = "tesseract"
    rotate: bool = True
    deskew: bool = True
    min_confidence: float = 3.0
    keep_folders: bool = False
    pdfa: bool = False  # PDF/A-2 for the documents that get a text layer (OCRmyPDF makes it)


def documents(in_dir: str) -> list[str]:
    """The ready document folders under in_dir (itself, its children, their children), oldest first."""
    found = []
    for root, dirs, files in os.walk(in_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        depth = os.path.relpath(root, in_dir).count(os.sep) + (root != in_dir)
        if depth >= 2:
            dirs[:] = []
        if "document.json" in files and root != in_dir:
            found.append(root)
            dirs[:] = []
    return sorted(found, key=lambda d: (_stamp(d), os.path.basename(d)))


def _stamp(folder: str) -> str:
    try:
        with open(os.path.join(folder, "document.json")) as f:
            return str(json.load(f).get("scanned") or "")
    except Exception:  # noqa: BLE001  (anything in a document.json; process() reports it)
        return ""


def _unique(directory: str, name: str, ext: str) -> str:
    path, n = os.path.join(directory, name + ext), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(directory, f"{name}_{n}{ext}")
    return path


class Status:
    """A JSON status file for a monitor: when (a heartbeat, at least every HEARTBEAT seconds),
    last_ok and last_file (the last PDF written), last_error (the last document moved to .failed or
    written without its text layer; cleared by the next clean one), failed (documents in IN/.failed,
    left for a person) and waiting (ready documents older than WAITING_AFTER seconds, which should
    not be: the step is stuck). And counts that only go up, for long-term statistics: pdfs (written),
    unreadable (moved to .failed), no_text (written without the text layer they asked for); they go
    on from the file's last values after a restart. Written atomically (a temporary file renamed)."""

    COUNTS = ("pdfs", "unreadable", "no_text")

    def __init__(self, path: str, in_dir: str) -> None:
        self.path, self.in_dir = path, in_dir
        self.data: dict[str, object] = {"last_ok": "", "last_file": "", "last_error": ""}
        self.counts = dict.fromkeys(self.COUNTS, 0)
        try:
            with open(path) as f:
                before = json.load(f)
            for k in self.COUNTS:
                self.counts[k] = max(0, int(before.get(k) or 0))
            self.data.update({k: str(before.get(k) or "") for k in ("last_ok", "last_file", "last_error")})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.written = 0.0

    def ok(self, name: str, error: str = "") -> None:
        self.counts["pdfs"] += 1
        self.counts["no_text"] += bool(error)
        self.data.update(last_ok=_now(), last_file=name, last_error=error)
        self.write(force=True)

    def failed(self, error: str) -> None:
        self.counts["unreadable"] += 1
        self.data["last_error"] = error
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.written < HEARTBEAT:
            return
        failed_dir = os.path.join(self.in_dir, FAILED)
        failed = len([d for d in os.listdir(failed_dir) if not d.startswith(".")]) if os.path.isdir(failed_dir) else 0
        cutoff = time.time() - WAITING_AFTER
        waiting = sum(1 for d in documents(self.in_dir) if _mtime(d) < cutoff)
        data = {"when": _now(), **self.data, "failed": failed, "waiting": waiting, **self.counts}
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
            self.written = time.monotonic()
        except OSError as e:
            log.warning("status file %s not written (%s)", self.path, e)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _mtime(folder: str) -> float:
    try:
        return os.stat(os.path.join(folder, "document.json")).st_mtime
    except OSError:
        return time.time()


def process(folder: str, out_dir: str, opts: Options, workdir: str) -> tuple[str, str]:
    """One document folder into a PDF: its path, and why its text layer is missing ("" when it is not).
    Raises ValueError or OSError for an unreadable document (the caller moves it aside); the folder
    itself is left for the caller."""
    with open(os.path.join(folder, "document.json")) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError("document.json is not an object")
    dpi = doc.get("resolution") or 300
    if isinstance(dpi, float) and dpi.is_integer():
        dpi = int(dpi)
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 50 <= dpi <= 2400:
        raise ValueError(f"resolution {dpi!r}: whole dpi, 50-2400")
    names = doc.get("pages") or sorted(p for p in os.listdir(folder) if PAGE_RE.match(p))
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and PAGE_RE.match(n) for n in names):
        raise ValueError(f"no pages, or page names not like page-0001.jpg: {names!r}")
    pages = []
    for n in names:
        with open(os.path.join(folder, n), "rb") as f:
            pages.append(f.read())
    turns, angles = [], []
    for page in pages:
        turn, angle = straighten(
            page, dpi, rotate=opts.rotate, deskew=opts.deskew, min_confidence=opts.min_confidence, workdir=workdir
        )
        turns.append(turn)
        angles.append(angle)
    data = pdf.build(pages, dpi, turns, angles)
    function = str(doc.get("function") or os.path.basename(os.path.dirname(folder))).lower()
    # The printer function's own wish (scankeyd's FUNC_<function>=...,ocr) decides; the folder list
    # only for documents that carry none.
    wants_text = (
        doc["ocr"] is True
        if isinstance(doc.get("ocr"), bool)
        else (function in opts.ocr_folders or "*" in opts.ocr_folders)
    )
    no_text = ""
    if opts.ocr_languages and wants_text and ocr.available():
        try:
            data = ocr.add_text_layer(data, opts.ocr_languages, workdir, engine=opts.engine, pdfa=opts.pdfa)
        except ocr.OcrError as e:
            no_text = f"{os.path.basename(folder)}: written without text, OCR failed ({e})"
            log.warning("%s", no_text)
    if opts.keep_folders and not FUNCTION_RE.match(function):
        raise ValueError(f"function {function!r} is not a plain folder name")
    target_dir = os.path.join(out_dir, function) if opts.keep_folders else out_dir
    os.makedirs(target_dir, exist_ok=True)
    path = _unique(target_dir, os.path.basename(folder), ".pdf")
    part = os.path.join(target_dir, f".{os.path.basename(path)}.part")
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, path)
    return path, no_text


def load_stuck(in_dir: str) -> set[str]:
    """The folders recorded as stuck, those still there."""
    try:
        with open(os.path.join(in_dir, STUCK)) as f:
            names = json.load(f)
    except (OSError, ValueError):
        return set()
    if not isinstance(names, list):
        return set()
    return {p for n in names if isinstance(n, str) and os.path.isdir(p := os.path.join(in_dir, n))}


def _save_stuck(in_dir: str, stuck: set[str]) -> None:
    tmp = os.path.join(in_dir, STUCK + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(sorted(os.path.relpath(p, in_dir) for p in stuck if os.path.isdir(p)), f)
        os.replace(tmp, os.path.join(in_dir, STUCK))
    except OSError as e:
        log.error("%s not written (%s): a restart converts the stuck folders again", STUCK, e)


def unwritable(in_dir: str) -> list[str]:
    """IN and its function folders that this user cannot change: a document's folder goes from there
    once its PDF is written, so each must be writable."""
    dirs = [in_dir] + [
        os.path.join(in_dir, d)
        for d in sorted(os.listdir(in_dir))
        if not d.startswith(".") and os.path.isdir(os.path.join(in_dir, d))
    ]
    return [d for d in dirs if not os.access(d, os.W_OK | os.X_OK)]


def _fail(folder: str, in_dir: str, why: str) -> None:
    dest_dir = os.path.join(in_dir, FAILED)
    os.makedirs(dest_dir, exist_ok=True)
    dest = _unique(dest_dir, os.path.basename(folder), "")
    os.replace(folder, dest)
    log.error("%s not converted (%s); moved to %s", os.path.basename(folder), why, dest)


def run_once(
    in_dir: str,
    out_dir: str,
    opts: Options,
    stop: threading.Event | None = None,
    status: Status | None = None,
    stuck: set[str] | None = None,
) -> int:
    """Every ready document now; how many PDFs were written. stuck: folders that could not be removed
    or moved aside, skipped so that none is converted twice; kept in IN/.tidyscan-stuck.json over
    restarts (read here when not given)."""
    written = 0
    stuck = load_stuck(in_dir) if stuck is None else stuck
    for folder in documents(in_dir):
        if stop is not None and stop.is_set():
            break
        if folder in stuck:
            continue
        with tempfile.TemporaryDirectory(prefix="tidyscan-") as work:
            try:
                path, no_text = process(folder, out_dir, opts, work)
            except Exception as e:  # noqa: BLE001  (any document, however malformed, must not stop the rest)
                why = (
                    str(e) if isinstance(e, (ValueError, OSError, pdf.UnsupportedPage)) else f"{type(e).__name__}: {e}"
                )
                where = f"moved to {FAILED}"
                try:
                    _fail(folder, in_dir, why)
                except OSError as e2:
                    log.error("%s not converted (%s) and not moved aside (%s)", os.path.basename(folder), why, e2)
                    stuck.add(folder)
                    _save_stuck(in_dir, stuck)
                    where = "not moved aside"
                if status:
                    status.failed(f"{os.path.basename(folder)}: {where} ({why})")
                continue
        try:
            shutil.rmtree(folder)
        except OSError as e:
            log.error("%s written, but its folder is not removed (%s); not converted again", folder, e)
            stuck.add(folder)
            _save_stuck(in_dir, stuck)
        written += 1
        log.info("written: %s", os.path.relpath(path, out_dir))
        if status:
            status.ok(os.path.basename(path), no_text)
    if status:
        status.write()
    return written


def run(
    in_dir: str,
    out_dir: str,
    opts: Options,
    interval: float = 5.0,
    stop: threading.Event | None = None,
    status: Status | None = None,
) -> None:
    """Watches in_dir until stop is set (SIGTERM in the command)."""
    stop = stop or threading.Event()
    opts.rotate = opts.rotate and orientation.available()
    opts.deskew = opts.deskew and skew.available()
    log.info(
        "watching %s -> %s: turn %s, straighten %s, text layer for %s",
        in_dir,
        out_dir,
        "on" if opts.rotate else "off",
        "on" if opts.deskew else "off",
        (
            f"{', '.join(opts.ocr_folders)} ({opts.ocr_languages}, {opts.engine})"
            if ocr.available()
            else "none (no OCRmyPDF)"
        )
        if opts.ocr_folders and opts.ocr_languages
        else "none",
    )
    stuck = load_stuck(in_dir)
    while not stop.is_set():
        try:
            run_once(in_dir, out_dir, opts, stop, status, stuck)
        except OSError as e:  # the input or output volume gone for a moment: try again
            log.error("watching failed (%s); retrying", e)
        stop.wait(interval)
