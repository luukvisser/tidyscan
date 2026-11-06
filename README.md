# tidyscan

Scanned pages turned upright and straightened into a PDF, losslessly, and
optionally made searchable.

- **Upright**: Tesseract's orientation detection decides the quarter turn,
  only when it is sure and the script is one you allow (Latin by default); an
  unsure upside-down page is settled by reading it both ways round.
- **Straight**: a page laid crooked on the glass (up to 45 degrees) is
  measured on its text lines and checked by measuring again.
- **Lossless**: JPEG pages go into the PDF byte for byte, 1-bit pages
  Flate-compressed; the turn is the page's `/Rotate`, the skew a drawing
  matrix. Nothing is resampled. Standard library only.
- **Searchable** (optional): a text layer by OCRmyPDF, read by Tesseract, or by
  PaddleOCR (PP-OCRv5 on OpenVINO, CPU) for receipts.

When anything is unsure, the page is kept as scanned. Written for the scans of
[scankeyd](https://github.com/luukvisser/scankeyd) (the Scan key of Brother
network printers to a folder), and usable for any scanned pages.

## Install

    pip install tidyscan                # pages to PDF only
    pip install 'tidyscan[straighten]'  # plus Pillow: turning upright and straightening
    pip install 'tidyscan[ocr]'         # plus OCRmyPDF: the text layer
    pip install 'tidyscan[paddle]'      # plus RapidOCR and OpenVINO: PaddleOCR

Python 3.11 or newer, on Linux. Turning and straightening also need the
`tesseract` program with its `osd` data, OCR its language data (Debian:
`tesseract-ocr tesseract-ocr-osd tesseract-ocr-nld ...`). PaddleOCR needs its
models once (checked by sha256, never downloaded by themselves):

    tidyscan models fetch    # into ~/.local/share/tidyscan/models
    tidyscan models path     # where they are looked up, and whether they are there

## Use

    tidyscan --ocr eng+nld -o out.pdf page1.jpg page2.pbm
    tidyscan --ocr nld --engine paddle -o receipt.pdf receipt.jpg

JPEG and PBM pages go in as they are. Other formats (PNG, TIFF, every page of
a multi-page TIFF) are read with Pillow and converted first, to PBM when
black-and-white, else to JPEG (quality 95): for those the PDF holds the
conversion. Options: `--dpi` (300), `--no-rotate`, `--no-deskew`,
`--min-confidence` (3), `--engine`, `-q`. Exit code 0, or 1 for a page that is
not a JPEG or PBM; an OCR failure writes the PDF without text and says so.

```python
import tidyscan
from tidyscan import ocr, pdf

pages = [open("page1.jpg", "rb").read()]
turns, skews = zip(*(tidyscan.straighten(p, 300) for p in pages))
data = pdf.build(pages, 300, list(turns), list(skews))
data = ocr.add_text_layer(data, "eng+nld")  # raises ocr.OcrError; engine="tesseract" (default), "paddle", "auto"
```

A page is JPEG bytes, binary PBM (P4) bytes, or an object with `to_pbm()`.
`straighten()` never raises and logs to the `tidyscan` logger; `turn=` takes a
turn already known (from the scanner), `budget=` the seconds of Tesseract it
may spend on a page (300; what is not measured by then is kept as scanned).

### Which OCR engine

| engine | words found | words right | 1 page | each further page | memory |
|---|---|---|---|---|---|
| **Tesseract 5.5** (`--engine tesseract`, the default) | 90% | 87% | 3.6 s | 2.7 s | 0.15 GB |
| PaddleOCR PP-OCRv5 (`--engine paddle`) | 83% | 92% | 6.1 s | 9.9 s | 0.9 GB |

Tesseract reads letters, documents, forms and book pages better, faster and
in less memory. PaddleOCR reads receipts, photo captions and tables better
(it adds far fewer wrong words on tables), and phone photos of receipts that
Tesseract cannot read at all. `--engine auto` takes PaddleOCR when it is
installed and the languages are Latin-script ones, else Tesseract; Tesseract
also takes over a document PaddleOCR fails on. PaddleOCR's Latin recogniser
reads Dutch, English and some 30 other languages at once. Measured on 45 pages
in 188 scan-like variants, through the whole pipeline, on 2 cores of an Intel
N150: [docs/benchmarks.md](docs/benchmarks.md).

## As a service: `tidyscan watch`

`tidyscan watch IN OUT` turns document folders into PDFs, oldest first: a
folder `IN/<function>/<name>/` with the pages (`page-0001.jpg`,
`page-0002.pbm`, ...) and a `document.json` (optional `"resolution"`, default
300; `"pages"`, default the page files in order; `"ocr": true` for a text
layer), as [scankeyd](https://github.com/luukvisser/scankeyd) writes them.
Each becomes `OUT/<name>.pdf`, written as a hidden `.part` file and renamed;
then the folder goes. A document that cannot be read moves to `IN/.failed/`;
when OCR fails, the PDF is written without text. IN and its function folders
must be writable for this user, as each folder goes once its PDF is there.

| Option | Environment | Default | |
|---|---|---|---|
| `--ocr LANGS` | `OCR_LANGUAGES` | `eng` | text layer languages, e.g. `eng+nld` |
| `--engine` | `OCR_ENGINE` | `tesseract` | `tesseract`, `paddle` or `auto` |
| `--ocr-folders` | `OCR_FOLDERS` | `ocr` | for documents without an `"ocr"` field: the function folders that get a text layer (comma list, `all`, or empty for none) |
| `--no-rotate`, `--no-deskew` | `ROTATE`, `DESKEW` | `auto` | `on`, `off` or `auto` (on when Tesseract is there) |
| `--pdfa` | `PDFA` | `off` | PDF/A-2b for the documents with a text layer (still lossless: 1-bit pages become CCITT G4) |
| `--status FILE` | `STATUS_FILE` | none | a JSON status file for a monitor: `when`, `last_ok`, `last_file`, `last_error`, `failed`, `waiting`, and the counts `pdfs`, `unreadable`, `no_text` |
| `--keep-folders` | | off | `OUT/<function>/<name>.pdf` |
| `--interval` | | 5 | seconds between looks at IN |
| `--once` | | | convert what is there, then stop |

On/off settings take `on`/`off`, `true`/`false`, `yes`/`no` or `1`/`0`; any
other value stops the start with a message.

**Docker**: three images, built from this repository (`docker build ...`):
`--target base` (pages to PDF only, ~77 MB, no copyleft software), the default
`full` (with Tesseract and OCRmyPDF, ~480 MB) and `--target paddle` (with
PaddleOCR, ~1 GB, amd64 only). They run `tidyscan watch /in /out` as uid 1000:

```sh
docker build -t tidyscan .
docker run -d --network none --read-only --tmpfs /tmp --env-file scan.env \
  -v ./scans/new:/in -v ./scans/pdf:/out tidyscan
```

OCR languages for the image: `--build-arg OCR_LANGS="eng nld deu fra"`.

## Limits

- Turning upright only for scripts you allow (Latin by default); skew up to 45
  degrees. Photos and nearly blank pages are kept as scanned, and so are phone
  photos of receipts (Tesseract finds no text lines on them).
- At most 5 minutes of Tesseract per page; a page above 200 million pixels is
  not read at all.
- OCRmyPDF gets 600 s per document: a long document read by PaddleOCR (about
  10 s a page) may run out and is then read by Tesseract.
- PaddleOCR: Latin script only; OCRmyPDF must run in the same Python as
  tidyscan and RapidOCR (the engine is the OCRmyPDF plugin
  `tidyscan.ocrmypdf_paddle`, also usable on its own: `ocrmypdf --plugin
  tidyscan.ocrmypdf_paddle`). `TIDYSCAN_PADDLE_THREADS` sets its threads.

## How well it works

Measured with `tests/eval_deskew.py` on 1946 cases from 69 pages, each turned,
tilted or mirrored by a known amount (06-11-2026):

- **Upright**: of 381 text pages that needed a turn, 96% upright and none
  turned the wrong way (OCRmyPDF's `--rotate-pages`: 29%; Tesseract OSD alone:
  77%, with 53 wrong turns). No upright page was turned away.
- **Straight**: within 0.5 degrees on 100%, 100% and 96% of the pages tilted
  slightly, medium and steeply. jdeskew with `angle_max=45` measures as well
  but turns no page upright; it, OCRmyPDF, ImageMagick, unpaper and Leptonica
  straighten by rotating the image, which resamples it.

Details, the other tools and the pages: [docs/benchmarks.md](docs/benchmarks.md).

## Development

With [uv](https://docs.astral.sh/uv/):

    uv sync
    uv run ruff check && uv run ruff format --check && uv run ty check
    uv run python -m unittest tests.test_tidyscan

Tests that need Tesseract, OCRmyPDF or the PaddleOCR models are skipped when
those are missing. The benchmarks (`tests/eval_*.py`) download their pages and
are not part of the unit tests.

## Licence

BSD-3-Clause, see [LICENSE](LICENSE); the repository follows
[REUSE](https://reuse.software/). Written by Luuk Visser with Claude Code
(Claude Opus 5.5).

What the optional parts use is not included in this package: OCRmyPDF
(MPL-2.0), Tesseract, RapidOCR, OpenVINO, OpenCV and PaddleOCR's PP-OCRv5
models (Apache-2.0; the models come with their own notice). The `full` and
`paddle` images add Debian packages under their own licences, among them
Ghostscript (AGPL-3.0, used by OCRmyPDF); the `base` image has no copyleft
software.
