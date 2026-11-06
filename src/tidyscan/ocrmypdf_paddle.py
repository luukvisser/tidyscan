"""OCRmyPDF plugin: PaddleOCR (tidyscan.paddle) as OCRmyPDF's OCR engine.

    ocrmypdf --plugin tidyscan.ocrmypdf_paddle -l eng+nld in.pdf out.pdf
    ocrmypdf --plugin tidyscan.ocrmypdf_paddle --ocr-engine paddle ...     (OCRmyPDF 17)

OCRmyPDF still rasterises each page and builds the text layer; this engine only reads the page
image and returns hOCR with a box per word. Works with OCRmyPDF 16 and 17:

  16  get_ocr_engine() takes no options and returns this engine; the hOCR renderer draws the
      text layer, so check_options turns renderer auto into hocr.
  17  get_ocr_engine(options) returns this engine for --ocr-engine auto or paddle and None for
      any other choice, so the next plugin answers (tesseract, none). The text layer comes from
      fpdf2, which parses this hOCR (auto, and hocr, which 17 redirects to fpdf2); generate_ocr,
      17's other route, is not needed for that.

The pages come from tidyscan already turned and straightened, so orientation and deskew report
nothing to do. Tesseract's own plugin stays loaded (its options are part of OCRmyPDF's), but this
engine is the one asked: tryfirst puts it ahead of the built-in ones.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from ocrmypdf import OcrEngine, OrientationConfidence, hookimpl, pluginspec
from ocrmypdf.exceptions import BadArgsError, MissingDependencyError

from . import paddle

if TYPE_CHECKING:
    from argparse import ArgumentParser
    from pathlib import Path

    # OCRmyPDF 17's options model; 16 passes an argparse.Namespace with the same attributes.
    from ocrmypdf import OcrOptions

ENGINE_NAME = "paddle"

# OCRmyPDF 17 passes the options to get_ocr_engine and has --ocr-engine; 16 has neither, and its
# plugin manager refuses a hook implementation that asks for an argument the spec lacks.
SELECTABLE = "options" in inspect.signature(pluginspec.get_ocr_engine).parameters


def selected(options: OcrOptions) -> bool:
    """Whether this run asks for this engine: always on 16, --ocr-engine auto or paddle on 17."""
    return not SELECTABLE or getattr(options, "ocr_engine", "auto") in ("auto", ENGINE_NAME)


@hookimpl
def add_options(parser: ArgumentParser) -> None:
    """Offer `--ocr-engine paddle` on 17. argparse cannot extend choices through its API, so the
    existing action's list is extended (as OCRmyPDF's own EasyOCR plugin does)."""
    for action in parser._actions:
        if action.dest == "ocr_engine" and action.choices is not None:
            if ENGINE_NAME not in action.choices:
                action.choices = [*action.choices, ENGINE_NAME]
            return


@hookimpl
def check_options(options: OcrOptions) -> None:
    if not selected(options):
        return
    lacking = paddle.missing()
    if lacking:
        raise MissingDependencyError("PaddleOCR engine: missing " + ", ".join(lacking))
    if options.pdf_renderer == "sandwich":
        raise BadArgsError("the PaddleOCR engine makes hOCR; use --pdf-renderer auto")
    if not SELECTABLE and options.pdf_renderer == "auto":
        options.pdf_renderer = "hocr"  # 16; on 17 auto is fpdf2, which reads hOCR


class PaddleOcrEngine(OcrEngine):
    @staticmethod
    def version() -> str:
        return paddle.versions()

    @staticmethod
    def creator_tag(options: OcrOptions) -> str:
        return f"PaddleOCR PP-OCRv5 ({paddle.versions()})"

    def __str__(self) -> str:
        return f"PaddleOCR PP-OCRv5 ({paddle.versions()})"

    @staticmethod
    def languages(options: OcrOptions) -> set[str]:
        return set(paddle.LANGUAGES)

    @staticmethod
    def get_orientation(input_file: Path, options: OcrOptions) -> OrientationConfidence:
        return OrientationConfidence(angle=0, confidence=15.0)

    @staticmethod
    def get_deskew(input_file: Path, options: OcrOptions) -> float:
        return 0.0

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path, options: OcrOptions) -> None:
        doc, text = paddle.read(input_file)
        with open(output_hocr, "w", encoding="utf-8") as f:
            f.write(doc)
        with open(output_text, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path, options: OcrOptions) -> None:
        raise NotImplementedError("the PaddleOCR engine makes hOCR only")


if SELECTABLE:

    @hookimpl(tryfirst=True)
    def get_ocr_engine(options: OcrOptions | None) -> PaddleOcrEngine | None:
        if options is not None and not selected(options):
            return None
        return PaddleOcrEngine()
else:

    @hookimpl(tryfirst=True)
    def get_ocr_engine() -> PaddleOcrEngine:
        return PaddleOcrEngine()
