# Instructions for coding agents

tidyscan turns scanned pages upright and straightens them, losslessly, and
writes them into a (searchable) PDF. Orientation comes from Tesseract's
orientation detection and is stored as the PDF's `/Rotate`; skew is measured
from Tesseract's text-line baselines and applied as a drawing matrix, so the
stored image stays bit for bit what the scanner sent. OCR runs through
OCRmyPDF, with PaddleOCR (PP-OCRv5 on OpenVINO) as a plugin and Tesseract as
the fallback.

## Layout

- `src/tidyscan/`: `__init__.py` (`straighten()`), `orientation.py`,
  `skew.py`, `hocr.py` and `_budget.py` (turning and straightening), `pdf.py`
  (the PDF writer, standard library only), `ocr.py`, `paddle.py` and
  `ocrmypdf_paddle.py` (OCR and the OCRmyPDF plugin), `models.py` (the
  models), `watch.py` (the service), `__main__.py` (the commands),
  `_page.py` (what a page is).
- `docs/`: the benchmarks and the models.
- `tests/test_tidyscan.py`: the unit tests. `tests/eval_*.py`: evaluations
  on real pages that download their inputs; they are not unit tests and do
  not run in CI.
- `tools/models.py`: fetches the PaddleOCR models at fixed commits, checks
  their sha256 and converts them.

## Commands

    uv sync
    uv run ruff check && uv run ruff format --check
    uv run ty check
    uv run python -m unittest tests.test_tidyscan

Tests that need Tesseract, OCRmyPDF or the models are skipped when those are
missing (Debian: `tesseract-ocr tesseract-ocr-osd tesseract-ocr-nld`).

## Rules

- Never decode and encode a page image again: pages are embedded as they came
  (JPEG as is, 1-bit Flate-compressed), and turning and straightening happen
  in the PDF, not in the pixels.
- `pdf.py` and the PDF-only install use the standard library only; Pillow,
  OCRmyPDF and the OCR engines are optional extras.
- No network access at run time. Models and inputs are fetched by tools and
  evaluation scripts only, from pinned sources checked by sha256.
- A change to orientation, skew or OCR comes with a rerun of the evaluation
  scripts and their numbers in the pull request; the README's figures are
  updated in the same change, with the date and the machine.
- When in doubt, leave a page as scanned: a page turned or tilted the wrong
  way is worse than one left alone.
- Never commit real scans or documents with personal data, or third-party test
  pages; evaluation scripts fetch theirs.
- Do not change LICENSE, LICENSES/, REUSE.toml or the release workflows unless
  asked to.
- Code reads like the code around it: comment density, naming, idiom.

## Commits and pull requests

State AI assistance with an `Assisted-by:` trailer naming the tool and model,
for example `Assisted-by: Claude Code (claude-opus-5-5)`. The person who opens
the pull request must understand, have tested and be able to explain every
change; see [CONTRIBUTING.md](CONTRIBUTING.md).
