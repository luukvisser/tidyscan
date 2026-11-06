"""Measures OCR quality, time and memory on scan-like pages with a known text (not part of the unit tests).

    python3 tests/eval_ocr.py fetch DIR        downloads the inputs into DIR/src and checks their sha256
                                               (standard library only; the only step that uses the network)
    python3 tests/eval_ocr.py prepare DIR      makes the pages (DIR/pages: PNG at 300 dpi and the text,
                                               .txt) and their scan-like variants (DIR/variants)
    python3 tests/eval_ocr.py run DIR          straightens each variant, builds the PDF, adds the text layer
                                               with each engine, takes the text out with pdftotext and scores
                                               it; one JSON line per variant and engine in DIR/results.jsonl
    python3 tests/eval_ocr.py report DIR       the tables (Markdown) from DIR/results.jsonl

`run` takes --engines paddle,tesseract,best (the default: all three), --only SUBSTRING, --no-timing and
--results FILE (run and report; default results.jsonl), e.g. for a timing pass on a quiet machine.
It continues where an interrupted run stopped (rows already in the results file are skipped).

What `prepare` and `run` need: tidyscan with its paddle extra and the models, OCRmyPDF, Tesseract
with the eng, nld and osd data, pdftotext and pdftoppm (Debian: poppler-utils), Pillow; `prepare` also
unpacks the pinned pyarrow wheel (CPython 3.13, x86_64) to read CORD's parquet file. The paddle image
(`docker build --target paddle -t tidyscan:paddle .`) has all but poppler-utils; add them:

    FROM tidyscan:paddle
    USER 0
    RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
    USER 1000:1000

    python3 -I tests/eval_ocr.py fetch /tmp/bench          # on the host, with network
    docker run --rm --network none --cpuset-cpus 2,3 -e TIDYSCAN_PADDLE_THREADS=2 \\
      -v "$PWD:/src:ro" -v /tmp/bench:/bench --entrypoint sh IMAGE \\
      -c 'cd /src && nice -n 19 python3 tests/eval_ocr.py prepare /bench \\
          && nice -n 19 python3 tests/eval_ocr.py run /bench'
    python3 -I tests/eval_ocr.py report /tmp/bench

The pages (PAGES, with a document class each). None of them is in this repository; `fetch` takes them
from the publishers at the pinned URLs in SOURCES and refuses a file whose sha256 differs.
- Born-digital pages, whose exact text is known: the text is taken from the PDF with pdftotext, the
  page rendered from the same PDF at 300 dpi gray. English: US federal government works (public domain
  in the US, 17 U.S.C. 105); Dutch: official publications of the Dutch State (officielebekendmakingen.nl,
  no copyright, Auteurswet art. 11). Two of them are bent as a book or magazine page is near the spine,
  with the spine's shadow.
- Real receipts: CORD v2 (NAVER Clova, CC-BY-4.0), phone photos of Indonesian till receipts with real
  tilt, curl and creases. Only the item and total lines are annotated, so their precision means little.
- Real scanned forms with handwriting: FUNSD (Jaume et al. 2019; non-commercial research and
  educational use only, so it is fetched at run time and never committed), noisy, crooked scans of about
  100 dpi, enlarged here to 300 dpi; all words annotated.
- Pages drawn by this script from text written for it (fonts: IBM Plex, SIL OFL 1.1, Google Fonts at a
  fixed commit): two till receipts, an invoice, a letter with a signature, a bank statement in small print,
  a page with a photo (NASA, public domain, from scikit-image's data) and a nearly blank page.

The variants make each page look scanned, in a fixed and repeatable way (randomness only with a fixed
seed): a gray JPEG (quality 80) and a 1-bit page (hard threshold at 128, as the scanner sends it);
turned a quarter, half and three quarters; tilted by 2.5 (gray) and -4 degrees (1-bit); blurred, with
noise and tilted by 0.8 degrees. Receipts lie on an empty A4 scan, and the drawn ones also get two steep
cases (-33 degrees; upside down at 27) and two crumpled ones. The real scans and photos get gray and
1-bit only: their tilt is their own. See CLASS_CONDITIONS.

Scores. A word is a run of characters between white space. Both texts are NFKC-normalised and
typographic quotes and dashes become ASCII ones (pdftotext gives ', OCR often '); otherwise a word
must match exactly, case and punctuation included. Recall = words found / words in the page; precision
= words found / words read; both count each word as often as it occurs (multisets), in any order.
"loose" also ignores case and punctuation at the ends of a word.
Time is OCRmyPDF's run for a one-page document (each run starts OCRmyPDF and loads the engine);
memory is the peak of the summed resident memory of OCRmyPDF and its child processes, sampled every
50 ms. A timing phase at the end also runs a 10-page document per engine, for the cost of each
further page.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from PIL.ImageFont import FreeTypeFont

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

OBK = "https://zoek.officielebekendmakingen.nl/"
TESSDATA_BEST = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/e12c65a915945e4c28e237a9b52bc4a8f39a0cec/"
)
FONTS = "https://raw.githubusercontent.com/google/fonts/bbda7253fc363238b05d97515ca71f8ae49c4fa9/ofl/"
US_PD = "US federal government work, public domain (17 U.S.C. 105)"
NL_PD = "Dutch official publication, no copyright (Auteurswet art. 11)"
SOURCES = {  # file: (url, sha256, licence)
    "fw4--2020.pdf": (
        "https://www.irs.gov/pub/irs-prior/fw4--2020.pdf",
        "202f0432dfb665f62a50659aa9712a7a4b9e0e642a00257de9c329f85eb03a8c",
        US_PD,
    ),
    "mm7301a1-H.pdf": (
        "https://www.cdc.gov/mmwr/volumes/73/wr/pdfs/mm7301a1-H.pdf",
        "561aca7dde34336da8da9e6c6d42cc29b4fafe2ced6e043334a0e189fab23023",
        US_PD,
    ),
    "mm7201a1-H.pdf": (
        "https://www.cdc.gov/mmwr/volumes/72/wr/pdfs/mm7201a1-H.pdf",
        "aa0158646a13d2d9419839b0677344b79e43981a3fff4d04ad6ef3eb013b69fd",
        US_PD,
    ),
    "FR-2024-03-05.pdf": (
        "https://www.govinfo.gov/content/pkg/FR-2024-03-05/pdf/FR-2024-03-05.pdf",
        "4c284cd58d0801a06e38a087ec08b40917d20c759bd4f28dd2c5a953499013e1",
        US_PD,
    ),
    "kst-31839-1000.pdf": (
        OBK + "kst-31839-1000.pdf",
        "be601be892e397a1433ec4d66c3ce8d58e38c36066a36eb56f5a5ba632bf9ef8",
        NL_PD,
    ),
    "kst-36410-3.pdf": (
        OBK + "kst-36410-3.pdf",
        "0dcec1dd408edc551b362c93293469ab9bee00667871e9222a58286c1aca261a",
        NL_PD,
    ),
    "ah-tk-20232024-100.pdf": (
        OBK + "ah-tk-20232024-100.pdf",
        "a6c76a8e124aca162ed980dca7cba9bb997e6a9d121d0c0333bc6d01014a3f83",
        NL_PD,
    ),
    "stb-2024-1.pdf": (
        OBK + "stb-2024-1.pdf",
        "415eddab0fb44a1d1c177ba104801a96386ee1c0453e0e6fcbc69a4284748db3",
        NL_PD,
    ),
    "cord-v2-test.parquet": (
        "https://huggingface.co/datasets/naver-clova-ix/cord-v2/resolve/7f0115a4b758a71d6473b8d085751692da2fef98"
        "/data/test-00000-of-00001-9c204eb3f4e11791.parquet",
        "51c65f1788faff392abe2a0b55b023eb23e9be551c509138eaa3a832514224e7",
        "CC-BY-4.0 (CORD v2, NAVER Clova)",
    ),
    "funsd.zip": (
        "https://guillaumejaume.github.io/FUNSD/dataset.zip",
        "c31735649e4f441bcbb4fd0f379574f7520b42286e80b01d80b445649d54761f",
        "FUNSD: non-commercial research and educational use only; never commit",
    ),
    "pyarrow-21.0.0-cp313-cp313-manylinux_2_28_x86_64.whl": (
        "https://files.pythonhosted.org/packages/89/4b/7782438b551dbb0468892a276b8c789b8bbdb25ea5c5eb27faadd753e037"
        "/pyarrow-21.0.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        "69cbbdf0631396e9925e048cfa5bce4e8c3d3b41562bbd70c685a8eb53a91e61",
        "Apache-2.0 (only to read CORD's parquet file)",
    ),
    "astronaut.png": (
        "https://raw.githubusercontent.com/scikit-image/scikit-image/a160f384523ac70173d069954b7084bee79fb68e"
        "/skimage/data/astronaut.png",
        "88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5",
        "NASA photograph, public domain (per scikit-image's data documentation)",
    ),
    "IBMPlexMono-Regular.ttf": (
        FONTS + "ibmplexmono/IBMPlexMono-Regular.ttf",
        "6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46",
        "SIL OFL 1.1",
    ),
    "IBMPlexMono-Bold.ttf": (
        FONTS + "ibmplexmono/IBMPlexMono-Bold.ttf",
        "ac27abd6450a64dd94467580a02fe6235156d5b92f2926ebbc8e7489df64e0be",
        "SIL OFL 1.1",
    ),
    "IBMPlexSans-Variable.ttf": (
        FONTS + "ibmplexsans/IBMPlexSans%5Bwdth,wght%5D.ttf",
        "3b031aa4216174205bd8471f88a49b91f093169e9e87bd5262242bc5967fe2e3",
        "SIL OFL 1.1",
    ),
    "IBMPlexSerif-Regular.ttf": (
        FONTS + "ibmplexserif/IBMPlexSerif-Regular.ttf",
        "e882efa9c41949a528ac2369079ec5ef050c1c996bbd0bacce3c3326d44cf80d",
        "SIL OFL 1.1",
    ),
    "IBMPlexSerif-Bold.ttf": (
        FONTS + "ibmplexserif/IBMPlexSerif-Bold.ttf",
        "534c02c295999dd86e770457ece1d43db0de9256dd98bf741426f63ae904209e",
        "SIL OFL 1.1",
    ),
    "tessdata_best/eng.traineddata": (
        TESSDATA_BEST + "eng.traineddata",
        "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
        "Apache-2.0",
    ),
    "tessdata_best/nld.traineddata": (
        TESSDATA_BEST + "nld.traineddata",
        "92e7a1ad4bf8082e268de57c7823316ec024935702c6ed2a1e473b3a071aa733",
        "Apache-2.0",
    ),
}

# Document classes, as reported; the conditions each class's pages get.
FULL = (
    "gray",
    "bw",
    "gray-turn90",
    "bw-turn180",
    "gray-turn270",
    "gray-tilt2.5",
    "bw-tilt-4",
    "gray-noisy-tilt0.8",
)
CLASSES = {  # class: (description, conditions)
    "document": ("born-digital text pages: letters, reports, columns, small print", FULL),
    "table": ("born-digital tables", FULL),
    "form": ("born-digital form", FULL),
    "invoice-letter": ("drawn invoice and letter (logo, table, signature)", FULL),
    "statement": ("drawn bank statement in small print", FULL),
    "receipt-drawn": (
        "drawn till receipts on an A4 scan",
        FULL + ("gray-steep-33", "bw-turn180-steep27", "gray-crumpled", "bw-crumpled-tilt12"),
    ),
    "receipt-real": (
        "real receipts (CORD v2 phone photos) on an A4 scan; own tilt and creases; gray only (a hard 1-bit"
        " threshold turns a photo's dark background into black blobs, which no flatbed scan has)",
        ("gray",),
    ),
    "form-handwritten-real": ("real scanned forms with handwriting (FUNSD); own skew and noise", ("gray", "bw")),
    "book-curved": (
        "born-digital pages, strongly bent near the spine (lines dip up to 160 px, 13 mm), with its shadow",
        FULL[:2] + ("bw-turn180", "gray-tilt2.5"),
    ),
    "book-curved-mild": (
        "the same pages, mildly bent (lines dip up to 60 px, 5 mm, in the quarter nearest the spine)",
        FULL[:2] + ("bw-turn180", "gray-tilt2.5"),
    ),
    "photo": ("a page with a photo and a caption", FULL[:2] + ("bw-turn180", "gray-tilt2.5")),
    "nearly-blank": ("a nearly blank page (a page number only)", FULL[:2] + ("bw-turn180", "gray-tilt2.5")),
}
# condition: (kind, quarter turn counter-clockwise, tilt in degrees counter-clockwise, noisy, crumpled)
CONDITIONS = {
    "gray": ("gray", 0, 0.0, False, False),
    "bw": ("bw", 0, 0.0, False, False),
    "gray-turn90": ("gray", 90, 0.0, False, False),
    "bw-turn180": ("bw", 180, 0.0, False, False),
    "gray-turn270": ("gray", 270, 0.0, False, False),
    "gray-tilt2.5": ("gray", 0, 2.5, False, False),
    "bw-tilt-4": ("bw", 0, -4.0, False, False),
    "gray-noisy-tilt0.8": ("gray", 0, 0.8, True, False),
    "gray-steep-33": ("gray", 0, -33.0, False, False),
    "bw-turn180-steep27": ("bw", 180, 27.0, False, False),
    "gray-crumpled": ("gray", 0, 0.0, False, True),
    "bw-crumpled-tilt12": ("bw", 0, 12.0, False, True),
}
CORD_PAGES = 12  # the first test receipts at least 864 px wide
FUNSD_PAGES = 12  # the first test forms, by file name
# page id: (source, selector, language, class, what); source "pdf:FILE" (selector: page number),
# "curved:FILE" / "mild:FILE" (the same, bent), "draw" (drawn here), "cord" (selector: row),
# "funsd" (selector: file stem)
PAGES: dict[str, tuple[str, Any, str, str, str]] = {
    "en-form": ("pdf:fw4--2020.pdf", 1, "eng", "form", "a tax form (IRS W-4 2020): boxes, small print"),
    "en-table": ("pdf:fw4--2020.pdf", 4, "eng", "table", "a dense table of numbers (IRS W-4 2020, page 4)"),
    "en-2col": ("pdf:mm7301a1-H.pdf", 2, "eng", "document", "a two-column report page with footnotes (CDC)"),
    "en-table-report": ("pdf:mm7201a1-H.pdf", 4, "eng", "table", "a report page with a wide table (CDC)"),
    "en-3col-small": ("pdf:FR-2024-03-05.pdf", 101, "eng", "document", "three columns of small print (US FR)"),
    "nl-letter": ("pdf:kst-31839-1000.pdf", 1, "nld", "document", "a letter to parliament (Kamerbrief)"),
    "nl-dense": ("pdf:kst-36410-3.pdf", 2, "nld", "document", "a dense page of parliamentary text"),
    "nl-qa": ("pdf:ah-tk-20232024-100.pdf", 2, "nld", "document", "parliamentary questions and answers"),
    "nl-law": ("pdf:stb-2024-1.pdf", 1, "nld", "document", "a law in the Staatsblad: title page"),
    "nl-table": ("pdf:stb-2024-1.pdf", 3, "nld", "table", "a budget table (Staatsblad)"),
    "nl-receipt": ("draw", None, "nld", "receipt-drawn", "a till receipt, faint print"),
    "en-receipt": ("draw", None, "eng", "receipt-drawn", "a till receipt"),
    "nl-invoice": ("draw", None, "nld", "invoice-letter", "an invoice with a logo and a table"),
    "en-letter": ("draw", None, "eng", "invoice-letter", "a letter with a letterhead and a signature"),
    "nl-statement": ("draw", None, "nld", "statement", "a bank statement, 6-7 pt print"),
    "nl-photo": ("draw", None, "nld", "photo", "a page with a photo and a caption"),
    "nl-blank": ("draw", None, "nld", "nearly-blank", "an empty page with its page number"),
    "en-magazine-curved": ("curved:mm7301a1-H.pdf", 1, "eng", "book-curved", "a report's first page, bent"),
    "nl-book-curved": ("curved:kst-36410-3.pdf", 3, "nld", "book-curved", "a dense page, bent"),
    "en-magazine-mild": ("mild:mm7301a1-H.pdf", 1, "eng", "book-curved-mild", "a report's first page, bent a little"),
    "nl-book-mild": ("mild:kst-36410-3.pdf", 3, "nld", "book-curved-mild", "a dense page, bent a little"),
}
PAGES.update({f"cord-{i:02d}": ("cord", i, "ind", "receipt-real", "a receipt (CORD v2)") for i in range(CORD_PAGES)})
PAGES.update(
    {f"funsd-{i:02d}": ("funsd", i, "eng", "form-handwritten-real", "a form (FUNSD)") for i in range(FUNSD_PAGES)}
)
TIMING_PAGES = ("en-form", "en-table", "en-2col", "en-table-report", "en-3col-small", "nl-letter", "nl-dense")
TIMING_PAGES += ("nl-qa", "nl-law", "nl-table")
LANGUAGES = "nld+eng"  # as the scanner daemon reads them; PaddleOCR reads both without a choice
DPI = 300
A4 = (2480, 3508)
JPEG_QUALITY = 80
ENGINES = ("paddle", "tesseract", "best")

RECEIPTS = {
    "nl-receipt": """\
SUPERMARKT DE GRAANSCHUUR
Molenstraat 14
1234 AB Voorbeeldstad
Tel. 012-3456789
KvK 12345678
--------------------------------
Halfvolle melk 1 L           1,29
Volkoren brood               2,49
Bananen 1,08 kg              2,15
Jonge kaas plakken           3,79
Appelsap 1,5 L               2,39
2 x Yoghurt naturel  1,19    2,38
Afwasmiddel                  1,89
Statiegeld fles              0,25
Korting kaas                -0,50
--------------------------------
TOTAAL                      16,13
Betaald PIN                 16,13
--------------------------------
BTW      Excl.     BTW     Incl.
9%      13,23     1,19     14,42
21%      1,41     0,30      1,71
--------------------------------
Kassa 3  Bon 0412  14-03-2026 10:42
Bedankt en tot ziens!
""",
    "en-receipt": """\
CORNER HARDWARE STORE
27 Station Road
Exampletown EX1 2AB
VAT no. 123 4567 89
--------------------------------
Wood screws 4x40 (200)      6.49
Wall plugs 6 mm (100)       3.25
Masking tape 25 mm          2.10
2 x Paint brush 50 mm       7.98
Sandpaper assorted          4.75
Light bulb LED E27          3.99
Cable ties (50)             1.80
--------------------------------
SUBTOTAL                   30.36
Discount 10%               -3.04
TOTAL                      27.32
Card payment               27.32
--------------------------------
Items: 8
Till 2  Receipt 5531
03/14/2026 16:05
Thank you for shopping with us!
Keep this receipt for returns.
""",
}

TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
EDGE_PUNCT = re.compile(r"^[^\w€$%]+|[^\w€$%]+$")


def words(text: str) -> list[str]:
    return unicodedata.normalize("NFKC", text).translate(TRANSLATE).split()


def score(truth: str, read: str) -> dict[str, int]:
    t, r = collections.Counter(words(truth)), collections.Counter(words(read))
    tl = collections.Counter(w for w in (EDGE_PUNCT.sub("", x).casefold() for x in words(truth)) if w)
    rl = collections.Counter(w for w in (EDGE_PUNCT.sub("", x).casefold() for x in words(read)) if w)
    return {
        "truth": sum(t.values()),
        "read": sum(r.values()),
        "hits": sum((t & r).values()),
        "truth_loose": sum(tl.values()),
        "read_loose": sum(rl.values()),
        "hits_loose": sum((tl & rl).values()),
    }


# --- fetch --------------------------------------------------------------------------------------------


def fetch(root: str) -> None:
    for name, (url, sha, licence) in SOURCES.items():
        path = os.path.join(root, "src", name)
        if os.path.isfile(path) and _sha256(path) == sha:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"fetching {name} ({licence})", flush=True)
        with urllib.request.urlopen(url, timeout=300) as r:  # noqa: S310 (fixed https URLs)
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            sys.exit(
                f"{name}: sha256 {got}, expected {sha}; the publisher changed the file, so this set needs a new pin"
            )
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
    print("all inputs present and checked")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- prepare: the pages -------------------------------------------------------------------------------


def prepare(root: str) -> None:
    from PIL import Image

    pages, variants = os.path.join(root, "pages"), os.path.join(root, "variants")
    os.makedirs(pages, exist_ok=True)
    os.makedirs(variants, exist_ok=True)
    cord = funsd = None
    for pid, (source, sel, _lang, cls, _what) in PAGES.items():
        png, txt = os.path.join(pages, pid + ".png"), os.path.join(pages, pid + ".txt")
        if not os.path.isfile(png):
            if source.startswith(("pdf:", "curved:", "mild:")):
                img, text = render_pdf_page(root, source.split(":", 1)[1], sel, pages)
                if source.startswith("curved:"):
                    img = bend_near_spine(img)
                elif source.startswith("mild:"):
                    img = bend_near_spine(img, zone=0.25, dip=60)
            elif source == "draw":
                img, text = DRAWERS[pid](root, pid)
            elif source == "cord":
                cord = cord or cord_rows(root)
                img, text = cord[sel]
            else:
                funsd = funsd or funsd_forms(root)
                img, text = funsd[sel]
            img.save(png, dpi=(DPI, DPI))
            with open(txt, "w", encoding="utf-8") as f:
                f.write(text)
        page = Image.open(png).convert("L")
        small = cls.startswith("receipt")
        for cond in CLASSES[cls][1]:
            kind, turn, tilt, noisy, crumple = CONDITIONS[cond]
            out = os.path.join(variants, f"{pid}__{cond}." + ("jpg" if kind == "gray" else "pbm"))
            if os.path.isfile(out):
                continue
            img = scan_like(page, small, turn, tilt, noisy, crumple, seed=f"{pid}/{cond}")
            with open(out + ".part", "wb") as f:
                f.write(encode(img, kind))
            os.replace(out + ".part", out)
    print(f"{len(os.listdir(pages)) // 2} pages, {len(os.listdir(variants))} variants in {root}")


def render_pdf_page(root: str, src: str, number: int, work: str) -> tuple[PILImage, str]:
    from PIL import Image

    pdf = os.path.join(root, "src", src)
    stem = os.path.join(work, "render-tmp")
    subprocess.run(
        ["pdftoppm", "-r", str(DPI), "-gray", "-png", "-f", str(number), "-l", str(number), "-singlefile", pdf, stem],
        check=True,
    )
    img = Image.open(stem + ".png").convert("L")
    img.load()
    os.unlink(stem + ".png")
    text = subprocess.run(
        ["pdftotext", "-f", str(number), "-l", str(number), "-enc", "UTF-8", pdf, "-"], check=True, capture_output=True
    ).stdout.decode()
    return img, text


def bend_near_spine(img: PILImage, zone: float = 0.45, dip: float = 160) -> PILImage:
    """A page as it lies in an opened book: cut to its text with narrow margins; near the spine (the left
    edge) the lines bend down and crowd together, and the paper darkens into the spine's shadow."""
    from PIL import Image, ImageChops

    box = img.point(lambda v: 255 if v < 160 else 0).getbbox() or (0, 0, *img.size)  # a book page has narrow
    cut = img.crop((max(0, box[0] - 120), max(0, box[1] - 120), min(img.width, box[2] + 120), box[3]))  # margins
    img = Image.new("L", (cut.width, cut.height + 200), 255)  # room below: the bend moves lines down
    img.paste(cut, (0, 0))
    w, h = img.size
    width = zone * w  # the bent part: `zone` of the page's width, from the spine

    def src(x: float, y: float) -> tuple[float, float]:
        s = max(0.0, 1 - x / width) ** 2  # 1 at the spine, 0 from `zone` of the width on
        return x - 0.08 * w * s * zone / 0.45, y - dip * s * (0.3 + 0.7 * y / h)

    n, m = 24, 32
    mesh = []
    for i in range(n):
        for j in range(m):
            x0, x1 = w * i // n, w * (i + 1) // n
            y0, y1 = h * j // m, h * (j + 1) // m
            quad = (*src(x0, y0), *src(x0, y1), *src(x1, y1), *src(x1, y0))
            mesh.append(((x0, y0, x1, y1), quad))
    bent = img.transform(img.size, Image.Transform.MESH, mesh, Image.Resampling.BICUBIC, fillcolor=255)
    row = bytes(int(255 - 150 * max(0.0, 1 - x / (0.12 * w)) ** 2) for x in range(w))  # dark at the spine
    shade = Image.frombytes("L", (w, 1), row).resize((w, h))
    return ImageChops.multiply(bent, shade)


def cord_rows(root: str) -> list[tuple[PILImage, str]]:
    """The first CORD_PAGES test receipts at least 864 px wide, gray, scaled to a receipt's width at
    300 dpi (about 90 mm); the text is every annotated word."""
    from PIL import Image

    lib = os.path.join(root, "src", "pyarrow-21.0.0")
    if not os.path.isdir(lib):
        with zipfile.ZipFile(os.path.join(root, "src", "pyarrow-21.0.0-cp313-cp313-manylinux_2_28_x86_64.whl")) as z:
            z.extractall(lib)
    sys.path.insert(0, lib)
    import pyarrow.parquet as pq  # ty: ignore[unresolved-import]

    table = pq.read_table(os.path.join(root, "src", "cord-v2-test.parquet"))
    out = []
    for row in table.to_pylist():
        gt = json.loads(row["ground_truth"])
        if gt["meta"]["image_size"]["width"] < 864:
            continue
        img = Image.open(io.BytesIO(row["image"]["bytes"])).convert("L")
        scale = min(1060 / img.width, 3100 / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
        lines = [" ".join(w["text"] for w in line["words"]) for line in gt["valid_line"]]
        out.append((img, "\n".join(lines) + "\n"))
        if len(out) == CORD_PAGES:
            break
    return out


def funsd_forms(root: str) -> list[tuple[PILImage, str]]:
    """The first FUNSD_PAGES test forms, enlarged to letter size at 300 dpi; the text is every word."""
    from PIL import Image

    out = []
    with zipfile.ZipFile(os.path.join(root, "src", "funsd.zip")) as z:
        names = sorted(n for n in z.namelist() if n.startswith("dataset/testing_data/images/") and n.endswith(".png"))[
            :FUNSD_PAGES
        ]
        for name in names:
            img = Image.open(io.BytesIO(z.read(name))).convert("L")
            scale = 2550 / img.width
            img = img.resize((2550, round(img.height * scale)), Image.Resampling.BICUBIC)
            ann = json.loads(z.read(name.replace("/images/", "/annotations/").replace(".png", ".json")))
            text = "\n".join(" ".join(w["text"] for w in e["words"]) for e in ann["form"])
            out.append((img, text + "\n"))
    return out


class Sheet:
    """An A4 page at 300 dpi; everything written on it is kept as the page's text."""

    def __init__(self, root: str, size: tuple[int, int] = A4) -> None:
        from PIL import Image, ImageDraw

        self.root, self.img = root, Image.new("L", size, 255)
        self.draw = ImageDraw.Draw(self.img)
        self.lines: list[str] = []
        self._fonts: dict[tuple[str, int], FreeTypeFont] = {}

    def font(self, name: str, size: int) -> FreeTypeFont:
        from PIL import ImageFont

        if (name, size) not in self._fonts:
            file = {"sans": "IBMPlexSans-Variable.ttf", "serif": "IBMPlexSerif-Regular.ttf"}.get(name, name)
            file = {"serif-bold": "IBMPlexSerif-Bold.ttf", "mono": "IBMPlexMono-Regular.ttf"}.get(file, file)
            file = {"mono-bold": "IBMPlexMono-Bold.ttf"}.get(file, file)
            self._fonts[(name, size)] = ImageFont.truetype(os.path.join(self.root, "src", file), size)
        return self._fonts[(name, size)]

    def text(
        self, x: float, y: float, s: str, font: str = "sans", size: int = 38, fill: int = 0, anchor: str = "la"
    ) -> None:
        self.draw.text((x, y), s, font=self.font(font, size), fill=fill, anchor=anchor)
        self.lines.append(s)

    def para(
        self, x: float, y: float, width: float, s: str, font: str = "sans", size: int = 38, lead: float = 1.45
    ) -> float:
        """Wrapped text; returns the y below it."""
        f, line = self.font(font, size), ""
        for word in s.split():
            if line and f.getlength(line + " " + word) > width:
                self.text(x, y, line, font, size)
                y, line = y + size * lead, word
            else:
                line = f"{line} {word}" if line else word
        if line:
            self.text(x, y, line, font, size)
            y += size * lead
        return y

    def table(self, x: float, y: float, widths: list[int], rows: list[list[str]], size: int, right: set[int]) -> float:
        """A ruled table; the first row is the header (bold); columns in `right` are right-aligned."""
        h = int(size * 1.9)
        total = sum(widths)
        for r, row in enumerate(rows):
            if r <= 1 or r == len(rows):
                self.draw.line((x, y, x + total, y), fill=0, width=3 if r <= 1 else 2)
            cx = x
            for c, (cell, w) in enumerate(zip(row, widths, strict=True)):
                if cell:
                    font = "serif-bold" if r == 0 else "sans"
                    if c in right:
                        self.text(cx + w - 14, y + h * 0.22, cell, font, size, anchor="ra")
                    else:
                        self.text(cx + 14, y + h * 0.22, cell, font, size)
                cx += w
            y += h
        self.draw.line((x, y, x + total, y), fill=0, width=3)
        return y

    def result(self) -> tuple[PILImage, str]:
        return self.img, "\n".join(self.lines) + "\n"


def draw_receipt(root: str, pid: str) -> tuple[PILImage, str]:
    """A till receipt, 80 mm wide, in a monospaced font; the nld one printed faint."""
    lines = RECEIPTS[pid].splitlines()
    top, step = 60, 46
    s = Sheet(root, (945, top * 2 + step * (len(lines) + 1)))
    ink = 85 if pid.startswith("nl") else 0  # thermal print fades
    y = top
    for i, line in enumerate(lines):
        font, size = ("mono-bold", 40) if i == 0 else ("mono", 34)
        centred = i < 4 or line.endswith("!")
        s.text(945 / 2 if centred else 40, y, line, font, size, fill=ink, anchor="ma" if centred else "la")
        y += step + (12 if i == 0 else 0)
    return s.result()


def draw_invoice(root: str, pid: str) -> tuple[PILImage, str]:
    s = Sheet(root)
    d = s.draw
    d.ellipse((200, 180, 380, 360), fill=70)  # the logo: a leaf in a circle, no letters
    d.polygon([(290, 205), (345, 300), (290, 335), (235, 300)], fill=255)
    d.line((290, 230, 290, 330), fill=70, width=8)
    s.text(420, 205, "Groenbouw Hoveniers B.V.", "serif-bold", 64)
    s.text(420, 295, "Dorpsweg 112, 3456 CD Voorbeeldstad", "sans", 36)
    s.text(420, 345, "Telefoon 030-1234567  info@groenbouw.example", "sans", 36)
    y = 560
    for line in ("Mevrouw A. Bakker", "Kerklaan 7", "6789 EF Ergensdorp"):
        s.text(200, y, line, "sans", 42)
        y += 60
    s.text(200, 900, "FACTUUR", "serif-bold", 80)
    y = 1030
    for k, v in (
        ("Factuurnummer", "2026-0318"),
        ("Factuurdatum", "14 maart 2026"),
        ("Vervaldatum", "13 april 2026"),
        ("Klantnummer", "10442"),
    ):
        s.text(200, y, k, "sans", 38)
        s.text(700, y, v, "sans", 38)
        y += 56
    rows = [
        ["Omschrijving", "Aantal", "Prijs", "Bedrag"],
        ["Snoeien heg en struiken", "6 uur", "€ 48,50", "€ 291,00"],
        ["Afvoeren groenafval", "1", "€ 65,00", "€ 65,00"],
        ["Beplanting border (vaste planten)", "24", "€ 7,95", "€ 190,80"],
        ["Tuinaarde 40 liter", "10", "€ 4,25", "€ 42,50"],
        ["Voorrijkosten", "1", "€ 25,00", "€ 25,00"],
    ]
    y = s.table(200, y + 60, [1020, 260, 300, 300], rows, 38, {1, 2, 3})
    y += 40
    for k, v, bold in (
        ("Subtotaal", "€ 614,30", False),
        ("BTW 21% over € 381,00", "€ 80,01", False),
        ("BTW 9% over € 233,30", "€ 21,00", False),
        ("Totaal te betalen", "€ 715,31", True),
    ):
        s.text(1180, y, k, "serif-bold" if bold else "sans", 40)
        s.text(2080, y, v, "serif-bold" if bold else "sans", 40, anchor="ra")
        y += 62
    y = s.para(
        200,
        y + 120,
        1880,
        "Wij verzoeken u het totaalbedrag binnen 30 dagen over te maken op rekening NL00 TEST 0123 4567 89 ten "
        "name van Groenbouw Hoveniers B.V., onder vermelding van het factuurnummer. Heeft u vragen over deze "
        "factuur? Neem dan contact met ons op via het telefoonnummer hierboven.",
        "sans",
        38,
    )
    d.line((200, 3250, 2280, 3250), fill=90, width=2)
    s.para(
        200,
        3280,
        2080,
        "KvK 87654321  BTW-nummer NL001234567B01  Op al onze werkzaamheden zijn de algemene voorwaarden van "
        "toepassing. U vindt ze op onze website of ontvangt ze op verzoek per post.",
        "sans",
        26,
    )
    return s.result()


def draw_letter(root: str, pid: str) -> tuple[PILImage, str]:
    s = Sheet(root)
    d = s.draw
    d.rectangle((200, 190, 330, 320), fill=40)  # the logo: a book shape
    d.polygon([(220, 300), (265, 215), (310, 300)], fill=255)
    s.text(370, 200, "Riverside Community Library", "serif-bold", 62)
    s.text(370, 285, "14 Mill Lane, Exampletown EX4 5CD  Telephone 01234 567890", "sans", 34)
    d.line((200, 370, 2280, 370), fill=0, width=4)
    s.text(2280, 470, "12 March 2026", "serif", 42, anchor="ra")
    y = 560
    for line in ("Mr R. Thompson", "8 Orchard Close", "Exampletown EX2 9GH"):
        s.text(200, y, line, "serif", 42)
        y += 62
    s.text(200, 860, "Dear Mr Thompson,", "serif", 42)
    s.text(200, 960, "Your library membership renewal", "serif-bold", 44)
    y = 1060
    for p in (
        "Thank you for being a member of Riverside Community Library. Our records show that your membership "
        "will expire at the end of next month. To keep borrowing books, audiobooks and films without "
        "interruption, please renew your membership before 30 April 2026.",
        "You can renew at the front desk during opening hours, or by returning the enclosed form in the "
        "envelope provided. The annual fee remains unchanged at 24 pounds; members over 65 and students in "
        "full-time education pay half of this amount.",
        "This year we have extended our opening hours on Thursday evenings until 8 pm, and the reading room on "
        "the first floor now offers free wireless internet and four new study desks. We have also added a "
        "collection of large print books near the entrance.",
        "If you have any questions about your membership, or if your address has changed, please do not "
        "hesitate to contact us. We look forward to welcoming you again soon.",
    ):
        y = s.para(200, y, 2080, p, "serif", 42, 1.5) + 40
    s.text(200, y + 20, "Yours sincerely,", "serif", 42)
    rng = random.Random("signature")  # a signature: a scribble, not text
    x0, y0 = 220, y + 200
    pts = [(x0 + i * 9, y0 + 40 * math.sin(i / 3.1) + rng.uniform(-14, 14)) for i in range(70)]
    d.line(pts, fill=20, width=6, joint="curve")
    s.text(200, y + 330, "Margaret Ellis", "serif", 42)
    s.text(200, y + 392, "Head of Lending Services", "serif", 42)
    return s.result()


def draw_statement(root: str, pid: str) -> tuple[PILImage, str]:
    s = Sheet(root)
    s.text(200, 200, "Voorbeeldbank N.V.", "serif-bold", 56)
    s.text(2280, 210, "Rekeningoverzicht", "serif-bold", 48, anchor="ra")
    y = 340
    for k, v in (
        ("Rekening", "NL00 VOOR 0987 6543 21"),
        ("Periode", "1 februari 2026 t/m 28 februari 2026"),
        ("Beginsaldo", "€ 1.254,37"),
        ("Eindsaldo", "€ 2.031,58"),
    ):
        s.text(200, y, k, "sans", 30)
        s.text(560, y, v, "sans", 30)
        y += 44
    entries = [
        ("02-02", "Salaris februari Werkgever B.V.", "", "2.845,00"),
        ("02-02", "Huur woning februari", "925,00", ""),
        ("03-02", "Supermarkt De Graanschuur Voorbeeldstad", "16,13", ""),
        ("04-02", "Energie Noord termijn februari", "142,00", ""),
        ("05-02", "Zorgverzekering premie februari", "151,25", ""),
        ("06-02", "Bakkerij Van Dam", "8,40", ""),
        ("07-02", "Tankstation Ringweg", "61,87", ""),
        ("09-02", "Gemeente Voorbeeldstad belastingen termijn 2", "48,92", ""),
        ("10-02", "Overboeking naar spaarrekening", "250,00", ""),
        ("11-02", "Apotheek Centrum", "12,35", ""),
        ("12-02", "Telefoon en internet februari", "57,50", ""),
        ("13-02", "Waterbedrijf voorschot", "21,40", ""),
        ("14-02", "Restaurant De Haven", "64,80", ""),
        ("16-02", "Sportvereniging contributie kwartaal", "45,00", ""),
        ("17-02", "Teruggave belasting", "", "112,00"),
        ("18-02", "Boekhandel Plein", "24,95", ""),
        ("19-02", "Supermarkt De Graanschuur Voorbeeldstad", "53,62", ""),
        ("20-02", "Bouwmarkt Industrieweg", "37,49", ""),
        ("23-02", "Fietsenmaker reparatie", "28,50", ""),
        ("24-02", "Bibliotheek contributie", "24,00", ""),
        ("25-02", "Drogisterij Marktplein", "11,79", ""),
        ("26-02", "Ziekenhuis parkeren", "4,50", ""),
        ("27-02", "Kosten betaalpakket februari", "3,95", ""),
    ]
    rows = [["Datum", "Omschrijving", "Af", "Bij"]] + [list(e) for e in entries]
    y = s.table(200, y + 50, [220, 1340, 260, 260], rows, 28, {2, 3})
    y = s.para(
        200,
        y + 70,
        2080,
        "Dit overzicht is met zorg samengesteld. Controleer de bij- en afschrijvingen en meld onjuistheden "
        "binnen 13 maanden na de boekingsdatum. Het tegoed op deze rekening valt onder het "
        "depositogarantiestelsel tot een bedrag van 100.000 euro per persoon per bank. Bewaar dit overzicht "
        "zorgvuldig; het kan gebruikt worden als bewijsstuk voor uw administratie en uw belastingaangifte. "
        "Rente wordt berekend over het saldo aan het einde van iedere dag en eenmaal per maand bijgeschreven.",
        "sans",
        24,
        1.4,
    )
    return s.result()


def draw_photo_page(root: str, pid: str) -> tuple[PILImage, str]:
    from PIL import Image

    s = Sheet(root)
    s.text(200, 220, "Verslag van de open dag", "serif-bold", 60)
    photo = Image.open(os.path.join(root, "src", "astronaut.png")).convert("L").resize((1700, 1700))
    s.img.paste(photo, (390, 420))
    y = s.para(
        390,
        2180,
        1700,
        "Afbeelding 1. Een astronaut in ruimtepak, gefotografeerd voor de vlag. Bron: NASA, publiek domein.",
        "serif",
        36,
    )
    s.para(
        200,
        y + 80,
        2080,
        "Tijdens de open dag konden bezoekers foto's uit de ruimtevaart bekijken en vragen stellen aan de "
        "medewerkers van het museum.",
        "serif",
        42,
    )
    s.text(2280, 3330, "7", "serif", 36, anchor="ra")
    return s.result()


def draw_blank(root: str, pid: str) -> tuple[PILImage, str]:
    s = Sheet(root)
    s.text(2280, 3330, "Pagina 4 van 6", "sans", 34, anchor="ra")
    return s.result()


DRAWERS = {
    "nl-receipt": draw_receipt,
    "en-receipt": draw_receipt,
    "nl-invoice": draw_invoice,
    "en-letter": draw_letter,
    "nl-statement": draw_statement,
    "nl-photo": draw_photo_page,
    "nl-blank": draw_blank,
}


# --- prepare: the variants ----------------------------------------------------------------------------


def scan_like(img: PILImage, small: bool, turn: int, tilt: float, noisy: bool, crumple: bool, seed: str) -> PILImage:
    """The page as a scanner would see it; turns and tilts are counter-clockwise."""
    from PIL import Image, ImageChops, ImageFilter

    if crumple:
        img = crumpled(img, seed)
    if small:  # a receipt lies near the top-left corner of an otherwise empty A4 scan
        a4 = Image.new("L", A4, 255)
        r = img.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255) if tilt else img
        a4.paste(r, (250, 200))
        img, tilt = a4, 0.0
    if tilt:  # the glass is larger than the page: nothing of the page is cut off
        img = img.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    if turn:
        img = img.rotate(turn, expand=True)
    if noisy:
        img = img.filter(ImageFilter.GaussianBlur(1.2))
        rng = random.Random(seed)
        noise = Image.frombytes("L", img.size, rng.randbytes(img.width * img.height)).point(lambda v: v * 25 // 256)
        img = ImageChops.add(img, noise, scale=1.0, offset=-12)
    return img


def crumpled(img: PILImage, seed: str) -> PILImage:
    """Creased paper: a smooth random displacement of up to ~10 px and uneven shading."""
    from PIL import Image, ImageChops, ImageFilter

    rng = random.Random(seed)
    w, h = img.size
    n, m = 6, max(6, round(6 * h / w))
    nodes = [[(rng.uniform(-10, 10), rng.uniform(-10, 10)) for _ in range(m + 1)] for _ in range(n + 1)]

    def at(i: int, j: int) -> tuple[float, float]:
        dx, dy = nodes[i][j] if 0 < i < n and 0 < j < m else (0.0, 0.0)
        return w * i / n + dx, h * j / m + dy

    mesh = []
    for i in range(n):
        for j in range(m):
            box = (w * i // n, h * j // m, w * (i + 1) // n, h * (j + 1) // m)
            mesh.append((box, (*at(i, j), *at(i, j + 1), *at(i + 1, j + 1), *at(i + 1, j))))
    out = img.transform(img.size, Image.Transform.MESH, mesh, Image.Resampling.BICUBIC, fillcolor=255)
    shade = Image.frombytes("L", (n * 2, m * 2), bytes(rng.randint(205, 255) for _ in range(n * m * 4)))
    shade = shade.resize(img.size, Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(20))
    return ImageChops.multiply(out, shade)


def encode(img: PILImage, kind: str) -> bytes:
    from PIL import Image

    if kind == "bw":  # a hard threshold, as the printer does (as in eval_corpus.py)
        bw = img.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)
        return b"P4\n%d %d\n" % ((bw.width + 7) // 8 * 8, bw.height) + bytes(b ^ 0xFF for b in bw.tobytes())
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


# --- run ----------------------------------------------------------------------------------------------


class Peak:
    """Peak summed RSS (MB) of this process's descendants while the block runs."""

    def __enter__(self) -> Peak:
        self.peak, self._stop = 0, threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _poll(self) -> None:
        me = os.getpid()
        while not self._stop.is_set():
            parents: dict[int, int] = {}
            rss: dict[int, int] = {}
            for p in os.listdir("/proc"):
                if not p.isdigit():
                    continue
                try:
                    with open(f"/proc/{p}/status") as f:
                        st = f.read()
                except OSError:
                    continue
                pp = re.search(r"^PPid:\s+(\d+)", st, re.M)
                vm = re.search(r"^VmRSS:\s+(\d+)", st, re.M)
                if pp:
                    parents[int(p)] = int(pp.group(1))
                    rss[int(p)] = int(vm.group(1)) if vm else 0
            total = 0
            for pid in parents:
                q, seen = pid, 0
                while q in parents and q != me and seen < 64:
                    q, seen = parents[q], seen + 1
                if q == me and pid != me:
                    total += rss[pid]
            self.peak = max(self.peak, total // 1024)
            self._stop.wait(0.05)


class Fallbacks(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(record.getMessage())


def ocr_once(pdf: bytes, engine: str, best_dir: str, work: str) -> tuple[bytes | None, float, int, str]:
    from tidyscan import ocr

    env_before = os.environ.get("TESSDATA_PREFIX")
    if engine == "best":
        os.environ["TESSDATA_PREFIX"] = best_dir
    handler = Fallbacks()
    logging.getLogger("tidyscan").addHandler(handler)
    started = time.monotonic()
    try:
        with Peak() as peak:
            out: bytes | None = ocr.add_text_layer(
                pdf, LANGUAGES, work, engine="paddle" if engine == "paddle" else "tesseract"
            )
        note = "; ".join(handler.seen)
    except ocr.OcrError as e:
        out, note = None, f"failed: {e}"
    finally:
        logging.getLogger("tidyscan").removeHandler(handler)
        if env_before is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = env_before
    return out, time.monotonic() - started, peak.peak, note


def pdftotext(pdf: bytes, work: str) -> str:
    path = os.path.join(work, "out.pdf")
    with open(path, "wb") as f:
        f.write(pdf)
    return subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], check=True, capture_output=True).stdout.decode()


def best_tessdata(root: str) -> str:
    """tessdata_best's eng and nld, with the system's osd data and configs next to them (OCRmyPDF needs
    osd to be listed, and the hocr config to write its output)."""
    d = os.path.join(root, "src", "tessdata_best")
    if not os.path.isdir(os.path.join(d, "configs")):
        listed = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
        m = re.search(r'"(/[^"]+)"', listed.stdout + listed.stderr)
        if not m:
            sys.exit("cannot find Tesseract's tessdata directory (for osd.traineddata and configs)")
        shutil.copy(os.path.join(m.group(1), "osd.traineddata"), d)
        for sub in ("configs", "tessconfigs"):
            if os.path.isdir(os.path.join(m.group(1), sub)):
                shutil.copytree(os.path.join(m.group(1), sub), os.path.join(d, sub), dirs_exist_ok=True)
    return d


def run(root: str, engines: tuple[str, ...], only: str, timing: bool, results_name: str) -> None:
    import tidyscan
    from tidyscan import pdf as pdfmod

    logging.basicConfig(level=logging.ERROR)
    best_dir = best_tessdata(root)
    results = os.path.join(root, results_name)
    done = set()
    if os.path.isfile(results):
        with open(results) as f:
            done = {(r["variant"], r["engine"]) for r in map(json.loads, f) if "variant" in r}
    work = tempfile.mkdtemp(prefix="eval-ocr-")
    variants = sorted(v for v in os.listdir(os.path.join(root, "variants")) if only in v)
    started = time.monotonic()
    meta = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "load": os.getloadavg(),
        "cpus": len(os.sched_getaffinity(0)),
    }
    with open(results, "a") as out:
        out.write(json.dumps({"meta": meta, "versions": versions()}) + "\n")
        for name in variants:
            vid = name.rsplit(".", 1)[0]
            todo = [e for e in engines if (vid, e) not in done]
            if not todo:
                continue
            pid, cond = vid.split("__")
            cls = PAGES[pid][3] if pid in PAGES else "other"
            real = cls.endswith("-real")  # its own tilt, not known
            truth = open(os.path.join(root, "pages", pid + ".txt"), encoding="utf-8").read()
            with open(os.path.join(root, "variants", name), "rb") as f:
                page = f.read()
            t0 = time.monotonic()
            turn, angle = tidyscan.straighten(page, DPI, workdir=work)
            straighten_s = time.monotonic() - t0
            doc = pdfmod.build([page], DPI, [turn], [angle])
            kind, turn_applied, tilt_applied, _noisy, _crumple = CONDITIONS[cond]
            for engine in todo:
                layered, secs, peak, note = ocr_once(doc, engine, best_dir, work)
                row = {
                    "variant": vid,
                    "page": pid,
                    "class": cls,
                    "cond": cond,
                    "engine": engine,
                    "kind": kind,
                    "turn_applied": turn_applied,
                    "tilt_applied": None if real else tilt_applied,
                    "turn_found": turn,
                    "skew_found": round(angle, 3),
                    "straighten_s": round(straighten_s, 2),
                    "ocr_s": round(secs, 2),
                    "peak_mb": peak,
                    "load1": round(os.getloadavg()[0], 2),
                    "note": note,
                    **score(truth, pdftotext(layered, work) if layered else ""),
                }
                out.write(json.dumps(row) + "\n")
                out.flush()
                print(
                    f"{vid:42s} {engine:9s} recall {row['hits'] / max(1, row['truth']):6.1%} "
                    f"precision {row['hits'] / max(1, row['read']):6.1%} {secs:5.1f} s {peak:5d} MB"
                    f" (straighten {straighten_s:.1f} s: turn {turn}, skew {angle:+.2f}){' ' + note if note else ''}",
                    flush=True,
                )
        if timing:
            multi_page_timing(root, engines, best_dir, work, out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"{time.monotonic() - started:.0f} s")


def multi_page_timing(root: str, engines: tuple[str, ...], best_dir: str, work: str, out: io.TextIOBase) -> None:
    """A 1-page and a 10-page document (the gray upright pages, not straightened: they are upright)."""
    from tidyscan import pdf as pdfmod

    pages = [open(os.path.join(root, "variants", p + "__gray.jpg"), "rb").read() for p in TIMING_PAGES]
    for engine in engines:
        row: dict[str, object] = {"timing": engine, "pages": len(pages), "load1_before": os.getloadavg()[0]}
        for n in (1, len(pages)):
            doc = pdfmod.build(pages[:n], DPI, [0] * n, [0.0] * n)
            _, secs, peak, note = ocr_once(doc, engine, best_dir, work)
            row[f"s_{n}"], row[f"peak_{n}"] = round(secs, 2), peak
            if note:
                row["note"] = note
        out.write(json.dumps(row) + "\n")
        out.flush()
        print(f"timing {engine}: {row}", flush=True)


def versions() -> dict[str, str]:
    from importlib.metadata import version

    import tidyscan
    from tidyscan import paddle

    def first_line(cmd: list[str]) -> str:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return (p.stdout or p.stderr).splitlines()[0].strip()
        except (OSError, IndexError):
            return "?"

    return {
        "tidyscan": tidyscan.__version__,
        "ocrmypdf": version("ocrmypdf"),
        "tesseract": first_line(["tesseract", "--version"]),
        "paddle": paddle.versions() if not paddle.missing() else "missing",
        "pdftotext": first_line(["pdftotext", "-v"]),
    }


# --- report -------------------------------------------------------------------------------------------

LABEL = {"paddle": "PaddleOCR PP-OCRv5", "tesseract": "Tesseract (default data)", "best": "Tesseract (tessdata_best)"}


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "-"


def _recall_table(title: str, keys: list[str], key: str, by: dict[str, list[dict]], engines: list[str]) -> None:
    print(f"\n{title}\n")
    print(
        f"| {key} | pages | variants | words | " + " | ".join(f"{LABEL[e]}: recall / precision" for e in engines) + " |"
    )
    print("|---|---|---|---|" + "---|" * len(engines))
    for k in keys:
        cells, pages, variants, n = [], 0, 0, 0
        for e in engines:
            rs = [r for r in by[e] if r[key] == k]
            if not rs:
                cells.append("-")
                continue
            pages, variants = len({r["page"] for r in rs}), len(rs)
            n = sum(r["truth"] for r in rs) // variants
            h, t, rd = (sum(r[x] for r in rs) for x in ("hits", "truth", "read"))
            cells.append(f"{_pct(h, t)} / {_pct(h, rd)}")
        if variants:
            print(f"| {k} | {pages} | {variants} | {n} | " + " | ".join(cells) + " |")


def report(root: str, results_name: str) -> None:
    rows, timings, metas = [], [], []
    with open(os.path.join(root, results_name)) as f:
        for r in map(json.loads, f):
            (rows if "variant" in r else timings if "timing" in r else metas).append(r)
    engines = [e for e in ENGINES if any(r["engine"] == e for r in rows)]
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by[r["engine"]].append(r)
    print(f"{len({r['variant'] for r in rows})} page variants of {len({r['page'] for r in rows})} pages")
    for m in metas:
        print(f"  {m}")
    loads = [r["load1"] for r in rows if "load1" in r]
    if loads:
        print(f"  load average (1 min) during the run: median {statistics.median(loads):.2f}, max {max(loads):.2f}")

    print("\nAll variants (recall and precision over all words):\n")
    print("| engine | recall | precision | recall, loose | OCR time a page: median (max) | peak memory: median (max) |")
    print("|---|---|---|---|---|---|")
    for e in engines:
        rs = by[e]
        h, t, rd = (sum(r[k] for r in rs) for k in ("hits", "truth", "read"))
        hl, tl = sum(r["hits_loose"] for r in rs), sum(r["truth_loose"] for r in rs)
        secs, mem = [r["ocr_s"] for r in rs], [r["peak_mb"] for r in rs]
        print(
            f"| {LABEL[e]} | {_pct(h, t)} | {_pct(h, rd)} | {_pct(hl, tl)}"
            f" | {statistics.median(secs):.1f} s ({max(secs):.0f} s)"
            f" | {statistics.median(mem):.0f} MB ({max(mem)} MB) |"
        )
    notes = [r for r in rows if r["note"]]
    if notes:
        print(f"\nnotes on {len(notes)} runs: " + "; ".join(sorted({r["note"][:160] for r in notes})))

    _recall_table("By document class:", list(CLASSES), "class", by, engines)
    full: dict[str, list[dict]] = {  # the classes whose pages get every condition, so rows compare
        e: [r for r in rs if set(FULL) <= set(CLASSES.get(r["class"], ("", ()))[1])] for e, rs in by.items()
    }
    _recall_table("By condition (only the pages that get every condition):", list(CONDITIONS), "cond", full, engines)
    _recall_table("By page:", list(PAGES), "page", by, engines)

    print("\nTime and memory by class (median OCR time a page; median peak memory):\n")
    print("| class | " + " | ".join(LABEL[e] for e in engines) + " |")
    print("|---|" + "---|" * len(engines))
    for c in CLASSES:
        cells = []
        for e in engines:
            rs = [r for r in by[e] if r["class"] == c]
            if rs:
                secs, mem = statistics.median(r["ocr_s"] for r in rs), statistics.median(r["peak_mb"] for r in rs)
                cells.append(f"{secs:.1f} s, {mem:.0f} MB")
            else:
                cells.append("-")
        print(f"| {c} | " + " | ".join(cells) + " |")

    print("\nStraightening of these variants (tidyscan.straighten, before OCR; known turns and tilts only):\n")
    seen = {r["variant"]: r for r in rows}
    right = kept = wrong = 0
    errs, unsure = [], 0
    for r in seen.values():
        if r["tilt_applied"] is None:
            continue
        if r["turn_found"] == r["turn_applied"]:
            right += 1
        elif r["turn_found"] == 0:
            kept += 1
        else:
            wrong += 1
        if r["tilt_applied"] and not r["cond"].endswith("crumpled-tilt12") and r["class"] != "receipt-drawn":
            if r["skew_found"]:
                errs.append(abs(r["skew_found"] + r["tilt_applied"]))
            else:
                unsure += 1
    print(f"turn: {right} right, {kept} kept as scanned, {wrong} wrong")
    if errs:
        print(
            f"tilt (full pages): {len(errs)} measured, {unsure} kept as scanned; error median"
            f" {statistics.median(errs):.2f}, max {max(errs):.2f} degrees"
        )
    for r in sorted(seen.values(), key=lambda r: r["variant"]):
        if r["tilt_applied"] is not None and r["turn_found"] not in (0, r["turn_applied"]):
            print(f"  wrong turn: {r['variant']}: found {r['turn_found']}, applied {r['turn_applied']}")
    strs = [r["straighten_s"] for r in seen.values()]
    print(f"straighten time: median {statistics.median(strs):.1f} s, max {max(strs):.1f} s")

    if timings:
        print("\nOCR time of a document (OCRmyPDF, one run):\n")
        print("| engine | 1 page | 10 pages | each further page | peak memory, 10 pages |")
        print("|---|---|---|---|---|")
        for t in timings:
            n = t["pages"]
            print(
                f"| {LABEL[t['timing']]} | {t['s_1']:.1f} s | {t[f's_{n}']:.1f} s"
                f" | {(t[f's_{n}'] - t['s_1']) / (n - 1):.1f} s"
                f" | {t[f'peak_{n}']} MB |"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("step", choices=("fetch", "prepare", "run", "report"))
    ap.add_argument("dir")
    ap.add_argument("--engines", default=",".join(ENGINES))
    ap.add_argument("--only", default="", help="only variants whose file name contains this")
    ap.add_argument("--no-timing", action="store_true", help="skip the 1- and 10-page timing documents")
    ap.add_argument("--results", default="results.jsonl", help="file name in DIR for the results")
    a = ap.parse_args()
    if a.step == "fetch":
        fetch(a.dir)
    elif a.step == "prepare":
        prepare(a.dir)
    elif a.step == "run":
        engines = tuple(a.engines.split(","))
        if not set(engines) <= set(ENGINES):
            sys.exit(f"--engines: some of {','.join(ENGINES)}")
        run(a.dir, engines, a.only, not a.no_timing, a.results)
    else:
        report(a.dir, a.results)


if __name__ == "__main__":
    main()
