"""tidyscan [options] -o OUT.pdf PAGE...: page images into one straightened PDF.

Also: `tidyscan watch IN OUT` (document folders as scankeyd writes them into PDFs, as a
service), `tidyscan models fetch [DIR]` (download and check PaddleOCR's models, once) and
`tidyscan models path` (where they are looked up)."""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys

from . import __version__, models, ocr, orientation, pdf, skew, straighten


def _load(path: str) -> list[bytes]:
    """The pages of one input file: a JPEG or PBM as it is; anything Pillow reads converted, every
    frame of a multi-page file (TIFF) a page of its own."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith((b"\xff\xd8", b"P4")):
        return [data]
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        raise SystemExit(f"{path}: not JPEG or PBM (other formats need Pillow)") from None
    pages = []
    for img in ImageSequence.Iterator(Image.open(io.BytesIO(data))):
        out = io.BytesIO()
        if img.mode == "1":
            img.save(out, "PPM")  # mode 1 saves as binary PBM
        elif img.mode.startswith("I"):  # 16-bit gray scaled to 8 bits (32-bit values above 65535 clip)
            img.convert("I").point(lambda v: v / 256).convert("L").save(out, "JPEG", quality=95)
        else:
            img.convert("L" if img.mode in ("L", "LA") else "RGB").save(out, "JPEG", quality=95)
        pages.append(out.getvalue())
    return pages


def _dpi(text: str) -> int:
    value = int(text)
    if not 50 <= value <= 2400:
        raise argparse.ArgumentTypeError(f"{value}: 50-2400")
    return value


def models_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="tidyscan models", description="PaddleOCR's PP-OCRv5 models")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help=f"download the models from release {models.TAG} and check their sha256")
    f.add_argument("directory", nargs="?", help="where to put them (default: the per-user model directory)")
    sub.add_parser("path", help="where the models are looked up, and whether they are there")
    a = p.parse_args(argv)
    if a.cmd == "path":
        print(models.describe())
        return 0
    try:
        written = models.fetch(a.directory)
    except models.ModelError as e:
        print(f"tidyscan: {e}", file=sys.stderr)
        return 1
    for path in written:
        print(f"written: {path}")
    print("models present" if not written else f"{len(written)} file(s) fetched and checked")
    return 0


def watch_main(argv: list[str]) -> int:
    import signal
    import threading

    from . import watch

    p = argparse.ArgumentParser(
        prog="tidyscan watch",
        description="Document folders (IN/<function>/<name>/ with document.json, as scankeyd writes them) "
        "into PDFs in OUT, straightened and with a text layer where asked; each folder goes once its PDF "
        "is written. Turning and straightening need Tesseract, the text layer OCRmyPDF; without them "
        "it only makes the PDFs.",
    )
    p.add_argument("input", metavar="IN")
    p.add_argument("output", metavar="OUT")
    env = os.environ  # the defaults, from the settings file all the scan containers share (env_file)

    def on(name: str, default: str = "auto", auto: bool = True) -> bool:
        """An on/off setting: on, off (also true/false, yes/no, 1/0), and auto where allowed (on when
        the tool for it is there)."""
        value = env.get(name, default).strip().lower()
        if value in ("on", "true", "yes", "1") or (auto and value == "auto"):
            return True
        if value in ("off", "false", "no", "0"):
            return False
        raise SystemExit(f"tidyscan: {name}={value!r}: on or off" + (" or auto" if auto else ""))

    p.add_argument(
        "--ocr",
        metavar="LANGS",
        default=env.get("OCR_LANGUAGES", "eng"),
        help="text layer languages, e.g. eng+nld (default: $OCR_LANGUAGES, else eng)",
    )
    p.add_argument(
        "--ocr-folders",
        default=env.get("OCR_FOLDERS", "ocr"),
        help="for documents without an ocr wish in their document.json: comma list of function folders "
        "that get a text layer (default: $OCR_FOLDERS, else ocr; 'all' for every one, '' for none)",
    )
    p.add_argument(
        "--engine",
        choices=ocr.ENGINES,
        default=env.get("OCR_ENGINE", "tesseract").lower(),
        help="OCR engine (default: $OCR_ENGINE, else tesseract)",
    )
    p.add_argument("--no-rotate", action="store_true", help="do not turn pages upright")
    p.add_argument("--no-deskew", action="store_true", help="do not straighten skewed pages")
    p.add_argument("--min-confidence", type=float, default=3.0, help="orientation confidence to turn (default 3)")
    p.add_argument(
        "--pdfa",
        action="store_true",
        default=on("PDFA", "off", auto=False),
        help="PDF/A-2 for the documents that get a text layer (default: $PDFA, else off)",
    )
    p.add_argument("--keep-folders", action="store_true", help="OUT/<function>/<name>.pdf instead of OUT/<name>.pdf")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between looks at IN (default 5)")
    p.add_argument("--once", action="store_true", help="convert what is there now, then stop")
    p.add_argument(
        "--status",
        metavar="FILE",
        default=env.get("STATUS_FILE") or None,
        help="a JSON status file for a monitor (last PDF, last error, failed and waiting documents); "
        "default: $STATUS_FILE, else none",
    )
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)
    if a.engine not in ocr.ENGINES:  # argparse does not check a default against choices
        p.error(f"OCR_ENGINE={a.engine!r}: one of {', '.join(ocr.ENGINES)}")
    logging.basicConfig(level=logging.WARNING if a.quiet else logging.INFO, format="%(levelname)s %(message)s")
    folders = tuple(f.strip().lower() for f in a.ocr_folders.split(",") if f.strip())
    if folders == ("all",):
        folders = ("*",)  # every function folder
    opts = watch.Options(
        ocr_languages=a.ocr,
        ocr_folders=folders,
        engine=a.engine,
        rotate=not a.no_rotate and on("ROTATE"),
        deskew=not a.no_deskew and on("DESKEW"),
        min_confidence=a.min_confidence,
        keep_folders=a.keep_folders,
        pdfa=a.pdfa,
    )
    for d in (a.input, a.output):
        if not os.path.isdir(d):
            print(f"tidyscan: {d} is not a directory", file=sys.stderr)
            return 2
    bad = [a.output] if not os.access(a.output, os.W_OK | os.X_OK) else []
    bad += watch.unwritable(a.input)  # IN too: a document's folder goes once its PDF is written
    if bad:
        print(f"tidyscan: not writable for this user: {', '.join(bad)}", file=sys.stderr)
        return 2
    status = watch.Status(a.status, a.input) if a.status else None
    if a.once:
        opts.rotate = opts.rotate and orientation.available()
        opts.deskew = opts.deskew and skew.available()
        watch.run_once(a.input, a.output, opts, status=status)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())  # finish the document at hand, then stop
    watch.run(a.input, a.output, opts, a.interval, stop, status)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["models"]:
        return models_main(argv[1:])
    if argv[:1] == ["watch"]:
        return watch_main(argv[1:])
    p = argparse.ArgumentParser(prog="tidyscan", description=__doc__.split(":", 1)[1].split("\n")[0].strip())
    p.add_argument("pages", nargs="+", metavar="PAGE", help="JPEG or PBM (other formats via Pillow, as JPEG)")
    p.add_argument("-o", "--output", required=True, help="the PDF to write")
    p.add_argument("--dpi", type=_dpi, default=300, help="scan resolution, 50-2400 (default 300)")
    p.add_argument("--no-rotate", action="store_true", help="do not turn pages upright")
    p.add_argument("--no-deskew", action="store_true", help="do not straighten skewed pages")
    p.add_argument("--min-confidence", type=float, default=3.0, help="orientation confidence to turn (default 3)")
    p.add_argument(
        "--ocr",
        metavar="LANGS",
        help="add a text layer, e.g. eng+nld (needs ocrmypdf; the languages steer Tesseract only)",
    )
    p.add_argument(
        "--engine",
        choices=ocr.ENGINES,
        default="tesseract",
        help="OCR engine: tesseract (the default; letters and documents), paddle (PaddleOCR, if "
        "installed; receipts), or auto (paddle when available, else tesseract)",
    )
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if a.quiet else logging.INFO, format="%(message)s")
    pages = [page for path in a.pages for page in _load(path)]
    turns, angles = [], []
    for page in pages:
        turn, angle = straighten(
            page, a.dpi, rotate=not a.no_rotate, deskew=not a.no_deskew, min_confidence=a.min_confidence
        )
        turns.append(turn)
        angles.append(angle)
    try:
        data = pdf.build(pages, a.dpi, turns, angles)
    except pdf.UnsupportedPage as e:
        print(f"tidyscan: {e}", file=sys.stderr)
        return 1
    if a.ocr:
        try:
            data = ocr.add_text_layer(data, a.ocr, engine=a.engine)
        except ocr.OcrError as e:
            print(f"tidyscan: OCR failed ({e}); written without text", file=sys.stderr)
    with open(a.output, "wb") as f:
        f.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
