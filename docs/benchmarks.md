# Benchmarks

All figures were measured on an Intel N150 (4 cores, CPU only, 16 GB), the
machine tidyscan was written for, with the scripts in `tests/`. They fetch
their pages from pinned URLs and check each one's sha256; none of the pages is
in this repository.

## Text recognition

`tests/eval_ocr.py`, 05-11-2026: 45 pages in 188 scan-like variants. Each
variant goes through the whole pipeline (straighten, PDF, OCRmyPDF) and the
text is taken from the PDF with pdftotext. Words found is the words found /
the words on the page (recall); words right is the words found / the words read
(precision). Each word counts as often as it occurs, with case and
punctuation, in any order. OCR ran on 2 of the 4 cores.

| engine | words found | words right | 1-page document: median (range) | each further page | peak memory: 1 page, 10 pages |
|---|---|---|---|---|---|
| PaddleOCR PP-OCRv5 (mobile detector + Latin recogniser, OpenVINO, CPU) | 83% | 92% | 6.1 s (2.9-26 s) | 9.9 s | 0.9 GB, 1.7 GB |
| **Tesseract 5.5**, default data | 90% | 87% | 3.6 s (0.8-24 s) | 2.7 s | 0.15 GB, 0.5 GB |
| Tesseract 5.5, tessdata_best (eng, nld) | 88% | 86% | 4.4 s (0.8-30 s) | 5.8 s | 0.17 GB, 0.6 GB |

A 1-page document is one OCRmyPDF run, model loading included; the range runs
from a nearly blank page to a dense table. "Each further page" comes from a
10-page document of dense pages: PaddleOCR reads one page at a time, Tesseract
two at once.

By kind of page (words found / words right):

| pages (pages, variants) | PaddleOCR | Tesseract | Tesseract best |
|---|---|---|---|
| letters, reports, columns, small print (6, 48) | 93% / 96% | 98% / 98% | 98% / 98% |
| tables (3, 24) | 87% / 87% | 85% / 70% | 79% / 67% |
| a tax form (1, 8) | 84% / 96% | 90% / 95% | 92% / 96% |
| an invoice and a letter with letterhead, logo and signature (2, 16) | 92% / 94% | 99% / 99% | 99% / 99% |
| a bank statement in 6-7 pt print (1, 8) | 97% / 98% | 99% / 99% | 99% / 99% |
| till receipts on an A4 scan, incl. steep and crumpled (2, 24) | 91% / 99% | 86% / 93% | 86% / 93% |
| real receipts: phone photos from CORD v2 (12, 12) | 71% / 72% | 1% / 80% | 1% / 67% |
| real scanned forms with handwriting, FUNSD (12, 24) | 65% / 73% | 64% / 71% | 64% / 71% |
| book pages, mildly bent at the spine (2, 8) | 70% / 92% | 98% / 98% | 98% / 98% |
| book pages, strongly bent at the spine (2, 8) | 15% / 69% | 69% / 64% | 69% / 64% |
| a page with a photo and a caption (1, 4) | 96% / 97% | 62% / 100% | 62% / 100% |

CORD annotates only the item and total lines, so "words right" means little
there; Tesseract finds no text at all in a phone photo with a dark background.

What the set shows:
- PaddleOCR reads receipts and the caption next to a photo better. On tables it
  finds as many words as Tesseract, but adds far fewer wrong ones.
- Tesseract reads letters, documents and forms better, is faster (3.6 against
  6.1 s for a 1-page document, 2.7 against 9.9 s for each further page) and
  needs a sixth of the memory.
- Turns and tilts hardly matter, because the page is straightened first: over
  the 15 pages that get every variant (gray and 1-bit; turned 90, 180 or 270
  degrees; tilted 2.5 or -4 degrees; blurred with noise), words found stay
  within 90-92% for PaddleOCR and 91-94% for Tesseract.
- PaddleOCR loses lines that curve, as near a book's spine.
- tessdata_best reads no better than the default data here, and is slower.

Hence Tesseract as the default and PaddleOCR as the choice for receipts.
PaddleOCR (OpenVINO, OpenCV, RapidOCR and the models) takes the image from
about 480 MB to about 1 GB, so its image is built only on request.

The pages, 45 in all:
- 10 born-digital pages whose text is known exactly: US federal government
  works (public domain) and Dutch official publications (no copyright under
  Auteurswet art. 11);
- 4 bent pages: two more born-digital pages, each bent at a book's spine once
  mildly and once strongly;
- 12 real receipts from CORD v2 (CC-BY-4.0);
- 12 real scanned forms with handwriting from FUNSD (non-commercial research
  use only, so fetched at run time and never committed);
- 7 pages drawn by the script from text written for it: two till receipts, an
  invoice, a letter, a bank statement, a page with a public-domain photo, and
  a nearly blank page.

Each is made to look scanned as a gray JPEG and as a 1-bit page; most are also
turned, tilted and blurred with noise, the drawn receipts also steep or
crumpled. The script's docstring has the details:

    python3 -I tests/eval_ocr.py fetch DIR      # network: downloads and checks the inputs
    python3 tests/eval_ocr.py prepare DIR       # the pages and their 188 variants
    python3 tests/eval_ocr.py run DIR           # about 1 h 50 min on 2 cores of an N150
    python3 tests/eval_ocr.py report DIR        # the tables

## Turning upright and straightening

`tests/eval_deskew.py`, 06-11-2026 (tidyscan 0.3.0): 1946 cases from 69
pages, each turned, tilted or mirrored by a known amount, gray and
black-and-white, against other open-source tools. Its docstring has the
pages, their sources and the image it runs in.

### Turning upright

Of 381 text pages that needed a turn, tidyscan left 367 upright (96%) and
turned none the wrong way; the rest were kept as scanned. Of 763 upright text
pages it turned none away.

| | upright | turned wrongly |
|---|---|---|
| **tidyscan** | 96% | 0 |
| OCRmyPDF `--rotate-pages` (threshold 14) | 29% | 0 |
| Tesseract OSD at any confidence | 77% | 53 |
| RapidOrientation (a learned classifier) | 76% | 63 |

A turn needs a confidence of at least 3: real turns came with 5.8-6.9, which
OCRmyPDF's 14 would have refused. Photo and nearly blank pages are kept as
scanned; of 366 such cases, 2 were turned wrongly (a black-and-white chart with
slanted axis labels). No tool in the comparison detects a mirrored page;
tidyscan never made one worse.

### Straightening

| method | within 0.5 deg, small / medium / steep tilts | median error of what it corrected | made worse | time a page |
|---|---|---|---|---|
| **tidyscan** (baselines, checked) | 100% / 100% / 96% | 0.03 deg | 1 of 120 steep cases | 7.9 s (15-18 s turned a quarter) |
| jdeskew, `angle_max=45` | 100% / 100% / 100% | 0.03 deg | 0 | 1.0 s |
| jdeskew, default range (15 deg) | 100% / 100% / 50% | 0.03 deg | 12-15% of steep | 1.0 s |
| OCRmyPDF `--deskew` (Tesseract psm 2) | 99% / 65% / 2% | 0.04 deg | 2-3% of medium | 4.2 s |
| Leptonica `pixFindSkew` (sweep 7 deg) | 100% / 50% / 0% | 0.02 deg | 0 | 0.15 s |
| ImageMagick `-deskew 40%` | 100% / 50% / 0% | 0.22 deg | 57-58% of steep | 5.2 s |
| PyPI `deskew` | 66% / 79% / 49% | 0.40 deg | 7-23% | 1.6 s |
| unpaper | 55% / 25% / 0% | 0.40 deg | 0-7% | 5.7 s |

On upright text, table, receipt and landscape pages (15); small is 0.6-2.3
degrees, medium 4.7-8.3, steep 14.6-33.4. The other tools only measure the
skew and turn no page upright: over all cases of the text, table and receipt
pages (part of them also turned) jdeskew-45 ends right in 72-76%, tidyscan in
96-100%. With the scanner lid open, the dark surround misleads jdeskew-45 (11
of 32 right, up to 19.6 degrees off). On every case with an exact answer
(1510, including turned, mirrored, bent and artefact pages), tidyscan ended
right in 83% and made 1% worse (9 cases: a chart, a bent book page, one steep
brochure).
