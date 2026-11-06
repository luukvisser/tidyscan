"""Compares tidyscan's orientation and skew detection with other open-source tools (not part of the unit tests).

Every case is a real page turned, tilted and mirrored by a known amount, so each has an exact expected
answer. The tools and their versions are in tests/eval_deskew.Dockerfile; run everything but `fetch`
in that image, without network:

    python3 -I tests/eval_deskew.py fetch WORK          # on the host: pinned downloads, sha256-checked
    docker build -f tests/eval_deskew.Dockerfile -t tidyscan-deskew-bench tests
    B="nice -n 19 docker run --rm --network none --cpus 2 -e OMP_THREAD_LIMIT=1 -v $PWD:/repo:ro -v WORK:/work
       -v OCRSET:/ocrset:ro tidyscan-deskew-bench"
    $B python3 /repo/tests/eval_deskew.py pages /work    # source pages, 300 dpi gray
    $B python3 /repo/tests/eval_deskew.py cases /work --ocr-set /ocrset   # cases (gray JPEG, 1-bit PBM) + cases.jsonl
    $B python3 /repo/tests/eval_deskew.py run /work TOOL[,TOOL...] [--jobs 2]   # results/TOOL.jsonl, resumable
                                                         # TOOL: see TOOLS ("osd" is reported as osd-raw and osd-thr14)

OCRSET is a directory prepared by tests/eval_ocr.py (`fetch`, then `prepare`): its pages/ are the
shared page set (receipts, real CORD receipts and FUNSD forms, letters, bent book pages, ...).
Without --ocr-set only the own 24 pages are used.
    $B python3 /repo/tests/eval_deskew.py probe /work TOOL            # peak memory, fresh process
    $B python3 /repo/tests/eval_deskew.py report /work   # the tables (Markdown)

Conventions: a case is the upright page, optionally mirrored, then turned counter-clockwise by
quarter*90 + tilt degrees (Pillow's rotate, bicubic, the corners filled white). A tool's answer is
reduced to one correction, counter-clockwise degrees; the residual is what is left after it
(normalised to -180..180, or to -90..90 for a mirrored page, which no rotation can make right).
The pages are not in this repository (most are CC-BY-SA or of unclear reuse status); see SOURCES.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import math
import os
import re
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OMP = "https://raw.githubusercontent.com/ocrmypdf/OCRmyPDF/b457e9efa68c3a46be2470ffb222ed1bccc4dcbc/tests/resources/"
FONTS = "https://raw.githubusercontent.com/google/fonts/bbda7253fc363238b05d97515ca71f8ae49c4fa9/ofl/ibmplexmono/"
SKI = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.26.0/src/skimage/data/"
SOURCES = {  # file: (url, sha256, licence)
    "linn.png": (
        OMP + "linn.png",
        "dcd90f08a6b8a733612f818330153636c13283dd548e2e092cad1fe628ec7bf3",
        "GFDL-1.2+ or CC-BY-SA-3.0",
    ),
    "typewriter.png": (
        OMP + "typewriter.png",
        "6f7a83685a83af954e9672b3e2db3253af165513d2826d780a48e175742f4469",
        "GFDL-1.2+ or CC-BY-SA-1.0..3.0",
    ),
    "c03-29.pdf": (
        OMP + "c03-29.pdf",
        "c2ff83af7d028c95209cc7eebdf80fd5bb4cd292973ed6601e4bab7d1929f201",
        "public domain",
    ),
    "epson.pdf": (
        OMP + "epson.pdf",
        "3fce2d6e5baffec839ffc86b7c91903b139399392ea76a09bdfcca40cb8b5a4d",
        "CC-BY-SA-3.0",
    ),
    "francais.pdf": (
        OMP + "francais.pdf",
        "600c27f8dd2ef085a94d3500f6ccdb3b37f4b89aa8ffdf9a00a06a1367887e8b",
        "CC-BY-SA-4.0",
    ),
    "lichtenstein.pdf": (
        OMP + "lichtenstein.pdf",
        "d7f2f944de30876e21b10caa465bc617e8fbd00285bdd1fc648d39793ebd76f8",
        "GFDL-1.2+ or CC-BY-SA-3.0",
    ),
    "graph.pdf": (
        OMP + "graph.pdf",
        "11fa2cef6845341795dc4103e68b31df5ff4fc72a7b7fa42904eb22b9bd70613",
        "GFDL-1.2+ or CC-BY-SA-3.0",
    ),
    "rijksoverheid-publicaties.pdf": (
        "https://open.overheid.nl/repository/ronl-906efb5d895e5fb9cc1c28e198790a048077191f/1/pdf/overzicht-relevante-publicaties.pdf",
        "1eafc201d9347e470253cf663ce47d0c456bf4474ca30feedd3819524e3939b6",
        "CC0 (Rijksoverheid)",
    ),
    "kst-36200-VII-2.pdf": (
        "https://zoek.officielebekendmakingen.nl/kst-36200-VII-2.pdf",
        "9308dc57ff96733e152d5460267c44beb79b9d4d1aba3fb9b173ff164531fbfd",
        "Dutch parliamentary paper (budget bill); no explicit licence, not redistributed",
    ),
    "i1040tt--2020.pdf": (
        "https://www.irs.gov/pub/irs-prior/i1040tt--2020.pdf",
        "867023f30ad6ef321ab8ede326c4d082130d21b34abee00ffd72fbe46b527b4a",
        "US government work (public domain)",
    ),
    "fw4--2020.pdf": (
        "https://www.irs.gov/pub/irs-prior/fw4--2020.pdf",
        "202f0432dfb665f62a50659aa9712a7a4b9e0e642a00257de9c329f85eb03a8c",
        "US government work (public domain)",
    ),
    "astronaut.png": (
        SKI + "astronaut.png",
        "88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5",
        "public domain (NASA)",
    ),
    "coffee.png": (
        SKI + "coffee.png",
        "cc02f8ca188b167c775a7101b5d767d1e71792cf762c33d6fa15a4599b5a8de7",
        "CC0 (Rachel Michetti)",
    ),
    # for the two till receipts drawn by tests/eval_ocr.py (draw_receipt), shared with the OCR benchmark
    "IBMPlexMono-Regular.ttf": (
        FONTS + "IBMPlexMono-Regular.ttf",
        "6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46",
        "SIL OFL 1.1",
    ),
    "IBMPlexMono-Bold.ttf": (
        FONTS + "IBMPlexMono-Bold.ttf",
        "ac27abd6450a64dd94467580a02fe6235156d5b92f2926ebbc8e7489df64e0be",
        "SIL OFL 1.1",
    ),
}

A4 = (2480, 3508)
A4L = (3508, 2480)

# page id: (kind, how it is made). Kinds: text, table, receipt, landscape, rotblock, photo, little, blank.
PAGES = {
    "nl-text": ("text", "Dutch text, kst-36200-VII-2 p. 9"),
    "nl-table": ("table", "Dutch table, kst-36200-VII-2 p. 20"),
    "nl-list": ("text", "Dutch list of links, Rijksoverheid"),
    "nl-typewriter": ("landscape", "Dutch typewritten recipe (landscape image), OCRmyPDF typewriter.png"),
    "en-text": ("text", "English two-column text, IRS Form W-4 (2020) p. 2"),
    "en-form": ("table", "English form, IRS Form W-4 (2020) p. 1"),
    "en-book": ("text", "English book page with an illustration, OCRmyPDF c03-29"),
    "en-print": ("text", "English Wikipedia printout, OCRmyPDF epson"),
    "en-brochure": ("text", "English two-column brochure, OCRmyPDF linn"),
    "en-table": ("table", "English number table, IRS 1040 tax table (2020) p. 5"),
    "receipt-strip": ("receipt", "narrow strip (59 x 257 mm) of the IRS tax table on an A4 scan: receipt-like"),
    "receipt-nl": ("receipt", "Dutch till receipt, faint print, drawn by tests/eval_ocr.py, on an A4 scan"),
    "receipt-en": ("receipt", "English till receipt drawn by tests/eval_ocr.py, on an A4 scan"),
    "landscape-table": ("landscape", "the Dutch table of kst p. 20, wide, on a landscape A4"),
    "rotated-block": ("rotblock", "Dutch text page with a paragraph turned 90 degrees in the right margin"),
    "photo-text": ("photo", "IRS cover: large photo, title and two lines of text"),
    "photo-astronaut": ("photo", "photo only (NASA, public domain) on an A4 scan"),
    "photo-coffee": ("photo", "photo only (coffee cup on wood grain, CC0) on an A4 scan"),
    "photo-tiles": ("photo", "wavelet image tiles, OCRmyPDF lichtenstein, on an A4 scan"),
    "chart": ("little", "line chart with axis labels, OCRmyPDF graph, on an A4 scan"),
    "little-text": ("little", "French, nine lines in a large font, OCRmyPDF francais, on an A4 scan"),
    "toc": ("little", "IRS table of contents page: a few lines"),
    "blank-logo": ("blank", "only the IRS logo (seal and three letters) on an A4 scan"),
    "blank-line": ("blank", "only one heading line on an A4 scan"),
}
TEXT_KINDS = ("text", "table", "receipt", "landscape", "rotblock")

# not whole degrees: a tool that searches in whole-degree steps would otherwise look exact
SMALL, MEDIUM, STEEP = (0.6, 2.3), (4.7, 8.3), (14.6, 33.4)
TILTS = (0.0,) + tuple(s * t for t in SMALL + MEDIUM + STEEP for s in (1, -1))
TURN_TILTS = (0.0, -6.4)
COMBOS = ((180, -24.6), (180, 32.7), (90, 25.3), (270, -39.6), (90, -14.8))
MIRROR_PAGES = ("nl-text", "en-table", "receipt-en", "landscape-table")
MIRROR_TILTS = (0.0, 1.3, -6.4, 21.7)  # none, small, medium, steep
ARTEFACT_PAGES = ("nl-text", "en-text", "en-book", "en-table")
ARTEFACTS = ("lid-open", "off-glass", "spine", "show-through")
PAGE_ARTEFACTS = ("spine", "show-through")  # applied to the page before it is turned; the others to the scan
ARTEFACT_TILTS = (0.0, 1.3, -5.4, 19.6)


def tilt_class(t: float) -> str:
    a = round(abs(t), 3)
    return "none" if a == 0 else "small" if a <= 3 else "medium" if a <= 10 else "steep"


# ---------------------------------------------------------------------------------------------- fetch


def fetch(work: str) -> None:
    src = os.path.join(work, "src")
    os.makedirs(src, exist_ok=True)
    for name, (url, sha, _) in SOURCES.items():
        path = os.path.join(src, name)
        if not os.path.exists(path):
            with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (pinned https URLs)
                data = r.read()
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if got != sha:
            raise SystemExit(f"{name}: sha256 {got}, expected {sha}")
        print(f"ok {name}")


# ---------------------------------------------------------------------------------------------- pages


def _render(work: str, pdf: str, page: int, dpi: int = 300):  # noqa: ANN202
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "p")
        subprocess.run(
            [
                "pdftoppm",
                "-r",
                str(dpi),
                "-gray",
                "-png",
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                os.path.join(work, "src", pdf),
                out,
            ],
            check=True,
        )
        return Image.open(out + ".png").convert("L").copy()


def _on(size: tuple[int, int], img, at: tuple[int, int]):  # noqa: ANN001, ANN202
    from PIL import Image

    canvas = Image.new("L", size, 255)
    canvas.paste(img, at)
    return canvas


def pages(work: str) -> None:
    from PIL import Image

    out = os.path.join(work, "pages")
    os.makedirs(out, exist_ok=True)
    src = lambda n: os.path.join(work, "src", n)  # noqa: E731
    kst9, kst20 = _render(work, "kst-36200-VII-2.pdf", 9), _render(work, "kst-36200-VII-2.pdf", 20)
    irs1, irs5 = _render(work, "i1040tt--2020.pdf", 1), _render(work, "i1040tt--2020.pdf", 5)
    rijk = _render(work, "rijksoverheid-publicaties.pdf", 1)
    p = {
        "nl-text": kst9,
        "nl-table": kst20,
        "nl-list": rijk,
        "en-text": _render(work, "fw4--2020.pdf", 2),
        "en-form": _render(work, "fw4--2020.pdf", 1),
        "en-book": _render(work, "c03-29.pdf", 1),
        "en-print": _render(work, "epson.pdf", 1),
        "en-brochure": Image.open(src("linn.png")).convert("L"),
        "en-table": irs5,
        "photo-text": irs1,
        "toc": _render(work, "i1040tt--2020.pdf", 2),
    }
    tw = Image.open(src("typewriter.png")).convert("L")
    p["nl-typewriter"] = tw.resize((A4L[0], round(tw.height * A4L[0] / tw.width)), Image.Resampling.LANCZOS)
    p["receipt-strip"] = _on(A4, irs5.crop((160, 120, 860, 3200)), (150, 150))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import eval_ocr  # the OCR benchmark's receipts; it reads the fonts from work/src as fetched here

    for name, pid in (("receipt-nl", "nl-receipt"), ("receipt-en", "en-receipt")):
        p[name] = _on(A4, eval_ocr.draw_receipt(work, pid)[0], (250, 200))  # where eval_ocr puts them
    table = _render(work, "kst-36200-VII-2.pdf", 20, 450).crop((330, 570, 3390, 2820))  # 300-dpi box * 1.5
    p["landscape-table"] = _on(A4L, table, ((A4L[0] - table.width) // 2, (A4L[1] - table.height) // 2))
    block = kst20.crop((900, 2540, 2240, 3040)).rotate(90, expand=True)
    main = kst9.resize((round(kst9.width * 0.78), round(kst9.height * 0.78)), Image.Resampling.LANCZOS)
    rb = _on(A4, main, (0, 380))
    rb.paste(block, (A4[0] - block.width - 120, 900))
    p["rotated-block"] = rb
    for name, key in (("photo-astronaut", "astronaut.png"), ("photo-coffee", "coffee.png")):
        ph = Image.open(src(key)).convert("L")
        ph = ph.resize((ph.width * 3, ph.height * 3), Image.Resampling.BICUBIC)
        p[name] = _on(A4, ph, ((A4[0] - ph.width) // 2, 500))
    p["photo-tiles"] = _on(A4, _render(work, "lichtenstein.pdf", 1), (440, 500))
    graph = _render(work, "graph.pdf", 1)
    p["chart"] = _on(A4, graph.resize((2200, round(graph.height * 2200 / graph.width))), (140, 400))
    p["little-text"] = _on(A4, _render(work, "francais.pdf", 1), (300, 300))
    p["blank-logo"] = _on(A4, irs1.crop((180, 100, 460, 440)), (200, 180))
    p["blank-line"] = _on(A4, rijk.crop((220, 360, 1200, 480)), (220, 360))
    assert set(p) == set(PAGES), set(PAGES) ^ set(p)
    for name, img in p.items():
        img.save(os.path.join(out, name + ".png"), dpi=(300, 300))
        print(name, img.size)


# ---------------------------------------------------------------------------------------------- cases


def _artefact(img, kind: str, back):  # noqa: ANN001, ANN202
    """A scanner artefact: lid-open and off-glass in the scan's frame, spine and show-through on the page."""
    from PIL import Image, ImageChops, ImageOps

    w, h = img.size
    if kind == "lid-open":
        # the lid open: everything around the page dark, a soft shadow along the page's top and right
        canvas = Image.new("L", (w + 160, h + 160), 25)
        mask = img.point(lambda v: 255)  # the page's own area (white corners belong to it)
        canvas.paste(img, (80, 80), mask)
        grad = Image.linear_gradient("L").resize((w + 160, 120)).point(lambda v: 90 + v * 165 // 255)
        canvas = ImageChops.multiply(canvas, _on((w + 160, h + 160), grad.rotate(180), (0, 80)).point(lambda v: v))
        return canvas
    if kind == "off-glass":
        # a corner of the page off the glass: cut 12% on the right and 8% at the top
        return img.crop((0, int(h * 0.08), int(w * 0.88), h))
    if kind == "spine":
        # a book spine: a dark gradient along the left edge, 15% of the width
        band = int(w * 0.15)
        grad = Image.linear_gradient("L").rotate(90, expand=True).resize((band, h)).point(lambda v: 50 + v * 205 // 255)
        shade = Image.new("L", (w, h), 255)
        shade.paste(ImageOps.mirror(grad), (0, 0))
        return ImageChops.multiply(img, shade)
    if kind == "show-through":
        # the back page, mirrored, at a sixth of its contrast
        back = ImageOps.mirror(back.convert("L")).resize(img.size)
        faint = back.point(lambda v: 255 - (255 - v) // 6)
        return ImageChops.darker(img, faint)
    raise ValueError(kind)


def encode(img, colour: str) -> tuple[bytes, str]:  # noqa: ANN001
    from PIL import Image

    if colour == "bw":
        # a hard threshold, as a scanner's black-and-white mode; P4 PBM as tidyscan takes it
        bw = img.convert("L").point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)
        return b"P4\n%d %d\n" % ((bw.width + 7) // 8 * 8, bw.height) + bytes(b ^ 0xFF for b in bw.tobytes()), "pbm"
    out = io.BytesIO()
    img.convert("L").save(out, "JPEG", quality=90, dpi=(300, 300))
    return out.getvalue(), "jpg"


# The shared page set of tests/eval_ocr.py (45 pages, prepared there: `eval_ocr.py fetch DIR` and
# `eval_ocr.py prepare DIR`), passed to `cases` as --ocr-set DIR. Its classes: receipts drawn and real
# (CORD v2), real scanned forms (FUNSD), letters, an invoice, a statement, tables, bent book pages, a
# photo page and a nearly blank page. Real pages have their own, unknown skew: they are measured for
# consistency (the same page with a known extra tilt or turn must get the same correction plus that).
SET2_TILTS = (0.6, -8.3, 14.6, -33.4)  # on top of eval_ocr's own conditions, synthetic pages only
REAL_EXTRA = ((0, 4.7), (0, -8.3), (180, 0.0), (90, 0.0))  # (turn, extra tilt) for the real pages
REAL_CLASSES = ("receipt-real", "form-handwritten-real")
SET2_TEXT = (
    "document",
    "table",
    "form",
    "invoice-letter",
    "statement",
    "receipt-drawn",
    "book-curved",
    "book-curved-mild",
)


def _eval_ocr():  # noqa: ANN202
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import eval_ocr

    return eval_ocr


def case_list(ocr_set: bool = False) -> list[dict]:
    cases = []

    def add(page: str, group: str, mirror: str = "", quarter: int = 0, tilt: float = 0.0, artefact: str = "") -> None:
        for colour in ("gray", "bw"):
            cid = f"{page}_{colour}_{mirror or 'm0'}_{quarter}_{tilt:+.1f}_{artefact or 'clean'}"
            cases.append(
                dict(
                    id=cid,
                    page=page,
                    kind=PAGES[page][0],
                    group=group,
                    colour=colour,
                    mirror=mirror,
                    quarter=quarter,
                    tilt=tilt,
                    tclass=tilt_class(tilt),
                    artefact=artefact,
                )
            )

    for page, (kind, _) in PAGES.items():
        for t in TILTS:
            add(page, "tilt", tilt=t)
        for q in (90, 180, 270):
            for t in TURN_TILTS:
                add(page, "turn", quarter=q, tilt=t)
        if kind in TEXT_KINDS:
            for q, t in COMBOS:
                add(page, "combo", quarter=q, tilt=t)
    for page in MIRROR_PAGES:
        for m in ("H", "V"):
            for q in (0, 90, 180, 270):
                for i, t in enumerate(MIRROR_TILTS):
                    add(page, "mirror", mirror=m, quarter=q, tilt=t if (q // 90 + i) % 2 == 0 or not t else -t)
    for page in ARTEFACT_PAGES:
        for a in ARTEFACTS:
            for t in ARTEFACT_TILTS:
                add(page, "artefact", tilt=t, artefact=a)
    if ocr_set:
        cases += set2_cases()
    return cases


def set2_cases() -> list[dict]:
    eo = _eval_ocr()
    cases = []

    def add(
        pid: str,
        cls: str,
        group: str,
        colour: str,
        quarter: int,
        tilt: float,
        noisy: bool = False,
        crumple: bool = False,
        cond: str = "",
        base: str = "",
    ) -> None:
        cid = f"s2-{pid}_{colour}_m0_{quarter}_{tilt:+.1f}_" + (cond or "clean")  # s2-: page ids overlap
        cases.append(
            dict(
                id=cid,
                page=pid,
                kind=cls,
                group=group,
                colour=colour,
                mirror="",
                quarter=quarter,
                tilt=tilt,
                tclass=tilt_class(tilt),
                artefact="",
                set="ocr",
                noisy=noisy,
                crumple=crumple,
                cond=cond,
                real=cls in REAL_CLASSES,
                base=base,
            )
        )

    for pid, (_src, _sel, _lang, cls, _what) in eo.PAGES.items():
        conds = eo.CLASSES[cls][1]
        if cls in REAL_CLASSES:
            for cond in conds:  # "gray" and/or "bw": the page as it is, the base for the consistency check
                colour = eo.CONDITIONS[cond][0]
                base = f"s2-{pid}_{colour}_m0_0_+0.0_{cond}"
                add(pid, cls, "real", colour, 0, 0.0, cond=cond, base=base)
                for q, t in REAL_EXTRA:
                    add(pid, cls, "real", colour, q, t, cond=f"{cond}-extra", base=base)
            continue
        for cond in conds:
            colour, q, t, noisy, crumple = eo.CONDITIONS[cond]
            add(pid, cls, "ocr-cond", colour, q, t, noisy, crumple, cond=cond)
        for t in SET2_TILTS:
            for colour in ("gray", "bw"):
                add(pid, cls, "set2-tilt", colour, 0, t)
    return cases


def _back(cache: dict, work: str):  # noqa: ANN202
    from PIL import Image

    if "en-print" not in cache:
        cache["en-print"] = Image.open(os.path.join(work, "pages", "en-print.png")).convert("L")
    return cache["en-print"]


def make_cases(work: str, ocr_set: str | None) -> None:
    from PIL import Image, ImageOps

    out = os.path.join(work, "cases")
    os.makedirs(out, exist_ok=True)
    cache: dict[str, Image.Image] = {}
    cases = case_list(bool(ocr_set))
    for c in cases:
        if c.get("set") == "ocr":
            eo = _eval_ocr()
            key = "ocr:" + c["page"]
            if key not in cache:
                cache.clear()
                cache[key] = Image.open(os.path.join(ocr_set, "pages", c["page"] + ".png")).convert("L")
            img = eo.scan_like(
                cache[key],
                c["kind"].startswith("receipt"),
                c["quarter"],
                c["tilt"],
                c["noisy"],
                c["crumple"],
                seed=f"{c['page']}/{c['cond'] or c['id']}",
            )
            data, ext = encode(img, c["colour"])
            c["file"] = f"{c['id']}.{ext}"
            with open(os.path.join(out, c["file"]), "wb") as f:
                f.write(data)
            continue
        page = cache.get(c["page"]) or Image.open(os.path.join(work, "pages", c["page"] + ".png")).convert("L")
        cache[c["page"]] = page
        img = page
        if c["artefact"] in PAGE_ARTEFACTS:  # on the paper: turns with the page
            img = _artefact(img, c["artefact"], _back(cache, work))
        if c["mirror"] == "H":
            img = ImageOps.mirror(img)
        elif c["mirror"] == "V":
            img = ImageOps.flip(img)
        turn = c["quarter"] + c["tilt"]
        if turn:
            img = img.rotate(turn, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
        if c["artefact"] and c["artefact"] not in PAGE_ARTEFACTS:  # of the scanner: in the scan's frame
            img = _artefact(img, c["artefact"], None)
        data, ext = encode(img, c["colour"])
        c["file"] = f"{c['id']}.{ext}"
        with open(os.path.join(out, c["file"]), "wb") as f:
            f.write(data)
    with open(os.path.join(work, "cases.jsonl"), "w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    print(f"{len(cases)} cases")


# ---------------------------------------------------------------------------------------------- tools
# Each returns {"turn": clockwise quarter turn or None (the tool does not turn), "angle": counter-clockwise
# degrees or None (the tool does not deskew)}, plus details. 0 means: looked, and left as scanned.


def _gray(data: bytes):  # noqa: ANN202
    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(data)).convert("L"))


def _as_file(data: bytes, ext: str, tmp: str, want: str) -> str:
    """The case as a file of type want (png/pgm/pbm/jpg/src); conversion is not timed."""
    from PIL import Image

    if want == "src" or want == ext:
        path = os.path.join(tmp, f"in.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        return path
    img = Image.open(io.BytesIO(data))
    img = img.convert("1") if img.mode == "1" and want in ("png", "pbm") else img.convert("L")
    path = os.path.join(tmp, f"in.{want}")
    img.save(path, dpi=(300, 300)) if want == "png" else img.save(path)
    return path


def t_tidyscan(data: bytes, ext: str, tmp: str) -> dict:
    import tidyscan

    turn, angle = tidyscan.straighten(data, 300, workdir=tmp)
    return {"turn": turn, "angle": angle}


def t_tidyscan_orientation(data: bytes, ext: str, tmp: str) -> dict:
    from tidyscan import orientation

    return {"turn": orientation.orientation(data, tmp), "angle": None}


def _osd(path: str) -> tuple[int, float, str] | None:
    p = subprocess.run(["tesseract", path, "-", "--psm", "0"], capture_output=True, timeout=120)
    out = p.stdout.decode("utf-8", "replace")
    rot = re.search(r"^Rotate:\s*(\d+)", out, re.M)
    conf = re.search(r"^Orientation confidence:\s*([\d.]+)", out, re.M)
    script = re.search(r"^Script:\s*(\S+)", out, re.M)
    if p.returncode or not rot or not conf:
        return None
    return int(rot.group(1)) % 360, float(conf.group(1)), script.group(1) if script else ""


def t_osd(data: bytes, ext: str, tmp: str) -> dict:
    """Tesseract OSD (--psm 0) on the page as scanned. The report reads it two ways: osd-raw (its answer
    whatever the confidence) and osd-thr14 (OCRmyPDF's default --rotate-pages-threshold 14)."""
    r = _osd(_as_file(data, ext, tmp, "src"))
    return {"turn": r[0] if r else 0, "angle": None, "conf": r[1] if r else None, "script": r[2] if r else None}


_RO = None


def t_rapid_orientation(data: bytes, ext: str, tmp: str) -> dict:
    """RapidOrientation (an ONNX document-orientation classifier). Its label is the clockwise turn of
    the page's content, so the clockwise correction is 360 minus it (calibrated on nl-text turned
    0/90/180/270)."""
    global _RO
    import cv2

    if _RO is None:
        from rapid_orientation import RapidOrientation

        _RO = RapidOrientation()
    img = cv2.cvtColor(_gray(data), cv2.COLOR_GRAY2BGR)
    label, _ = _RO(img)
    return {"turn": (360 - int(label)) % 360, "angle": None, "label": label}


def t_ocrmypdf(data: bytes, ext: str, tmp: str) -> dict:
    """OCRmyPDF 16 itself: --rotate-pages --deskew (defaults), OCR switched off by --tesseract-timeout 0."""
    path = _as_file(data, ext, tmp, "jpg" if ext == "jpg" else "png")
    p = subprocess.run(
        [
            "ocrmypdf",
            "--rotate-pages",
            "--deskew",
            "--tesseract-timeout",
            "0",
            "--image-dpi",
            "300",
            "-j",
            "1",
            "--output-type",
            "pdf",
            "-v1",
            path,
            os.path.join(tmp, "out.pdf"),
        ],
        capture_output=True,
        timeout=600,
    )
    log = p.stderr.decode("utf-8", "replace")
    rot = re.search(r"Rasterize with \S+, rotation (\d+)", log)
    desk = re.search(r"Deskew angle: (-?[\d.]+)", log)
    facing = re.search(r"confidence ([\d.]+) - ([^\n]+)", log)
    if p.returncode or not rot:
        return {"turn": 0, "angle": 0.0, "error": f"exit {p.returncode}: {log[-300:]}"}
    ccw = int(rot.group(1)) % 360  # OCRmyPDF's correction, counter-clockwise
    return {
        "turn": (360 - ccw) % 360,
        "angle": float(desk.group(1)) if desk else 0.0,
        "conf": float(facing.group(1)) if facing else None,
    }


def t_deskew(data: bytes, ext: str, tmp: str) -> dict:
    """PyPI deskew (sbrunner): determine_skew with its defaults; the README rotates by the result."""
    from deskew import determine_skew

    a = determine_skew(_gray(data))
    return {"turn": None, "angle": 0.0 if a is None else float(a)}


def t_jdeskew(data: bytes, ext: str, tmp: str) -> dict:
    """jdeskew get_angle with its defaults (angle_max 15); the README rotates by the result."""
    from jdeskew.estimator import get_angle

    return {"turn": None, "angle": float(get_angle(_gray(data)))}


def t_jdeskew45(data: bytes, ext: str, tmp: str) -> dict:
    from jdeskew.estimator import get_angle

    return {"turn": None, "angle": float(get_angle(_gray(data), angle_max=45.0))}


_LEPT = None


def t_leptonica(data: bytes, ext: str, tmp: str) -> dict:
    """Leptonica pixFindSkew on the page made 1-bit at 130 (as pixDeskew does), and pixDeskew's own rule:
    corrected only with confidence >= 3 and at least 0.1 degrees. The angle is clockwise; sweep +-7."""
    global _LEPT
    if _LEPT is None:
        lib = ctypes.CDLL("libleptonica.so.6")
        lib.pixRead.restype, lib.pixRead.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        lib.pixConvertTo1.restype, lib.pixConvertTo1.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int]
        lib.pixFindSkew.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
        lib.pixDestroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        _LEPT = lib
    lib = _LEPT
    path = _as_file(data, ext, tmp, "png")
    pix = ctypes.c_void_p(lib.pixRead(path.encode()))
    pix1 = ctypes.c_void_p(lib.pixConvertTo1(pix, 130))
    angle, conf = ctypes.c_float(0), ctypes.c_float(0)
    err = lib.pixFindSkew(pix1, ctypes.byref(angle), ctypes.byref(conf))
    lib.pixDestroy(ctypes.byref(pix1))
    lib.pixDestroy(ctypes.byref(pix))
    if err or abs(angle.value) < 0.1 or conf.value < 3.0:
        return {"turn": None, "angle": 0.0, "raw": angle.value, "conf": conf.value}
    return {"turn": None, "angle": -angle.value, "conf": conf.value}


def t_unpaper(data: bytes, ext: str, tmp: str) -> dict:
    """unpaper 7 with its defaults; the angle it rotates by, from its verbose output (radians)."""
    path = _as_file(data, ext, tmp, "pbm" if ext == "pbm" else "pgm")
    out = os.path.join(tmp, "out." + path.rsplit(".", 1)[1])
    p = subprocess.run(["unpaper", "-v", "--overwrite", path, out], capture_output=True, timeout=600)
    log = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    rot = re.findall(r"^rotate \(\d+,\d+\): (-?[\d.]+)", log, re.M)
    if p.returncode:
        return {"turn": None, "angle": 0.0, "error": log[-300:]}
    return {"turn": None, "angle": sum(math.degrees(float(r)) for r in rot) if rot else 0.0}


def t_imagemagick(data: bytes, ext: str, tmp: str) -> dict:
    """ImageMagick 7 -deskew 40%; %[deskew:angle] is the skew it found (counter-clockwise positive)."""
    path = _as_file(data, ext, tmp, "src" if ext == "jpg" else "png")
    p = subprocess.run(
        ["magick", path, "-deskew", "40%", "-format", "%[deskew:angle]", "info:"], capture_output=True, timeout=600
    )
    try:
        return {"turn": None, "angle": -float(p.stdout.decode().strip())}
    except ValueError:
        return {"turn": None, "angle": 0.0, "error": p.stderr.decode("utf-8", "replace")[-300:]}


def t_cv_minarearect(data: bytes, ext: str, tmp: str) -> dict:
    """The common OpenCV recipe: Otsu threshold, minAreaRect of all ink pixels, angle folded to +-45."""
    import cv2
    import numpy as np

    g = _gray(data)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    pts = np.column_stack(np.where(th > 0))[:, ::-1].astype(np.float32)
    if len(pts) < 10:
        return {"turn": None, "angle": 0.0}
    a = cv2.minAreaRect(pts)[-1]  # the range differs between OpenCV versions: fold to -45..45
    a = (a + 45) % 90 - 45
    return {"turn": None, "angle": float(a)}


def t_cv_hough(data: bytes, ext: str, tmp: str) -> dict:
    """The other common OpenCV recipe: Canny + probabilistic Hough lines, median angle of the lines
    within 45 degrees of horizontal."""
    import cv2
    import numpy as np

    g = _gray(data)
    scale = 1000 / max(g.shape)
    small = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(small, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=80, minLineLength=small.shape[1] // 8, maxLineGap=10)
    if lines is None:
        return {"turn": None, "angle": 0.0}
    angles = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        a = (a + 90) % 180 - 90
        if abs(a) < 45:
            angles.append(a)
    if not angles:
        return {"turn": None, "angle": 0.0}
    return {"turn": None, "angle": float(statistics.median(angles))}


TOOLS = {
    "tidyscan": t_tidyscan,
    "tidyscan-orientation": t_tidyscan_orientation,
    "osd": t_osd,
    "rapid-orientation": t_rapid_orientation,
    "ocrmypdf": t_ocrmypdf,
    "deskew": t_deskew,
    "jdeskew": t_jdeskew,
    "jdeskew-45": t_jdeskew45,
    "leptonica": t_leptonica,
    "unpaper": t_unpaper,
    "imagemagick": t_imagemagick,
    "cv-minarearect": t_cv_minarearect,
    "cv-hough": t_cv_hough,
}
ORIENT_ONLY = ("tidyscan-orientation", "osd-raw", "osd-thr14", "rapid-orientation")  # osd: one run, read twice
SKEW_ONLY = ("deskew", "jdeskew", "jdeskew-45", "leptonica", "unpaper", "imagemagick", "cv-minarearect", "cv-hough")
FULL = ("tidyscan", "ocrmypdf")


def wanted(tool: str, c: dict) -> bool:
    """Skew-only tools skip the turned pages outside the mirror grid and the combos (they cannot turn)."""
    turned = c["group"] == "turn" or (c["group"] in ("ocr-cond", "real") and c["quarter"] != 0)
    return not (tool in SKEW_ONLY and turned)


# ---------------------------------------------------------------------------------------------- run


def _one(args: tuple[str, str, dict]) -> dict:
    work, tool, c = args
    sys.path.insert(0, os.path.join(REPO, "src"))
    data = open(os.path.join(work, "cases", c["file"]), "rb").read()
    with tempfile.TemporaryDirectory() as tmp:
        t0 = time.perf_counter()
        try:
            r = TOOLS[tool](data, c["file"].rsplit(".", 1)[1], tmp)
        except Exception as e:  # noqa: BLE001
            r = {"turn": 0, "angle": 0.0, "error": repr(e)[:300]}
        r["seconds"] = round(time.perf_counter() - t0, 3)
    r["id"] = c["id"]
    return r


def run(work: str, tool: str, jobs: int) -> None:
    res_dir = os.path.join(work, "results")
    os.makedirs(res_dir, exist_ok=True)
    path = os.path.join(res_dir, tool + ".jsonl")
    done = set()
    if os.path.exists(path):
        done = {json.loads(line)["id"] for line in open(path)}
    cases = [c for c in map(json.loads, open(os.path.join(work, "cases.jsonl"))) if wanted(tool, c)]
    todo = [c for c in cases if c["id"] not in done]
    print(f"{tool}: {len(todo)} of {len(cases)} to do", flush=True)
    t0 = time.monotonic()
    with open(path, "a") as f, ProcessPoolExecutor(jobs) as pool:
        for i, r in enumerate(pool.map(_one, [(work, tool, c) for c in todo], chunksize=1)):
            f.write(json.dumps(r) + "\n")
            f.flush()
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(todo)} {time.monotonic() - t0:.0f} s", flush=True)
    print(f"{tool}: {time.monotonic() - t0:.0f} s", flush=True)


def probe(work: str, tool: str) -> None:
    """Peak memory (this process and its largest child) and time over a fixed set of 6 cases."""
    ids = [
        "nl-text_gray_m0_0_+2.3_clean",
        "nl-text_bw_m0_0_-4.7_clean",
        "en-table_gray_m0_0_+14.6_clean",
        "receipt-en_bw_m0_0_-33.4_clean",
        "photo-coffee_gray_m0_0_+4.7_clean",
        "nl-typewriter_gray_m0_0_-2.3_clean",
    ]
    cases = {c["id"]: c for c in map(json.loads, open(os.path.join(work, "cases.jsonl")))}
    t0 = time.perf_counter()
    for i in ids:
        _one((work, tool, cases[i]))
    secs = time.perf_counter() - t0
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    print(
        json.dumps(
            {
                "tool": tool,
                "peak_mib_self": round(own),
                "peak_mib_child": round(child),
                "seconds_6_pages_incl_imports": round(secs, 1),
            }
        )
    )


# ---------------------------------------------------------------------------------------------- report


def norm(a: float, half: float = 180.0) -> float:
    return (a + half) % (2 * half) - half


def evaluate(c: dict, r: dict, tool: str, base_corr: float = 0.0) -> dict:
    """Residual turn of the page after the tool's correction, and the outcome. For a real page (unknown
    own skew) base_corr is the tool's correction of the page as it is, so the residual is relative to it."""
    mirrored = bool(c["mirror"])
    half = 90.0 if mirrored else 180.0
    before = norm(c["quarter"] + c["tilt"], half)
    turn = r.get("turn") or 0
    angle = r.get("angle") or 0.0
    correction = angle - turn - base_corr
    after = norm(before + correction, half)
    acted = bool(turn) or abs(angle) > 1e-9
    skew_err = abs(norm(after, 45.0))  # distance of the text lines to the nearest axis
    upright = abs(after) < 45
    # worse: further from upright in quarter turns, or as far in quarters with the lines more crooked
    # (a sideways page whose skew is corrected is not worse, though it stays sideways)
    q_before, q_after = abs(round(before / 90)), abs(round(after / 90))
    skew_before = abs(norm(before, 45.0))
    if abs(after) <= 0.5:
        outcome = "right"
    elif not acted:
        outcome = "left"
    elif q_after > q_before or (q_after == q_before and skew_err > skew_before + 0.25):
        outcome = "worse"
    else:
        outcome = "better"
    return dict(
        before=before,
        after=after,
        acted=acted,
        skew_err=skew_err,
        upright=upright,
        outcome=outcome,
        turned=bool(turn),
        seconds=r.get("seconds", 0.0),
        error=r.get("error"),
    )


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def _stats(rows: list[dict]) -> str:
    errs = [e["skew_err"] for e in rows if e["acted"] and e["upright"] or e["acted"] and e["skew_err"] is not None]
    return f"{statistics.median(errs):.2f} / {max(errs):.2f}" if errs else "- / -"


def report(work: str) -> None:
    cases = {c["id"]: c for c in map(json.loads, open(os.path.join(work, "cases.jsonl")))}
    res: dict[str, dict[str, dict]] = {}
    raw_all: dict[str, dict[str, dict]] = {}
    for tool in TOOLS:
        path = os.path.join(work, "results", tool + ".jsonl")
        if not os.path.exists(path):
            continue
        raws = {r["id"]: r for r in map(json.loads, open(path))}
        variants = {tool: raws}
        if tool == "osd":
            variants = {
                "osd-raw": raws,
                "osd-thr14": {i: r | {"turn": r["turn"] if (r.get("conf") or 0) >= 14 else 0} for i, r in raws.items()},
            }
        for name, rr in variants.items():
            raw_all[name] = rr
            res[name] = {}
            for i, r in rr.items():
                c = cases[i]
                if c.get("real"):
                    b = rr.get(c["base"])
                    if b is None:
                        continue
                    base_corr = (b.get("angle") or 0.0) - (b.get("turn") or 0)
                    e = evaluate(c, r, name, base_corr)
                else:
                    e = evaluate(c, r, name)
                res[name][i] = e | {"case": c, "raw": r}
    print("results per tool: " + json.dumps({t: len(v) for t, v in res.items()}))
    _report_skew(res)
    _report_orientation(res)
    _report_mirror(res)
    _report_artefacts(res)
    _report_combo(res)
    _report_nontext(res)
    _report_classes(res)
    _report_real(res)
    _report_checks(res)
    _report_time(res)
    _report_failures(res)


def _row(tool: str, rows: list[dict], skew_only: bool = False) -> str:
    n = len(rows)
    acted = [e for e in rows if e["acted"]]
    errs = [e["skew_err"] if skew_only else abs(e["after"]) for e in acted]
    wrong = sum(1 for x in errs if x > 0.5)
    med = f"{statistics.median(errs):.2f}" if errs else "-"
    worst = f"{max(errs):.2f}" if errs else "-"
    left = sum(1 for e in rows if not e["acted"] and abs(e["before"]) > 0.5)
    worse = sum(1 for e in rows if e["outcome"] == "worse")
    right = sum(1 for e in rows if (e["skew_err"] if skew_only else abs(e["after"])) <= 0.5)
    return (
        f"| {tool} | {n} | {_pct(right, n)} | {med} | {worst} | {_pct(wrong, len(acted))} | "
        f"{_pct(left, n)} | {_pct(worse, n)} |"
    )


HDR = (
    "| tool | cases | right (<= 0.5 deg) | median error of the corrected | worst | wrong > 0.5 of the corrected |"
    " left as scanned | made worse |\n|---|---|---|---|---|---|---|---|"
)


def _report_skew(res: dict) -> None:
    print("\n## Skew, upright pages with text (group tilt, text kinds), by tilt class and colour\n")
    for tclass in ("none", "small", "medium", "steep"):
        for colour in ("gray", "bw"):
            print(f"\n### tilt {tclass}, {colour}\n\n{HDR}")
            for tool in FULL + SKEW_ONLY:
                if tool not in res:
                    continue
                rows = [
                    e
                    for e in res[tool].values()
                    if e["case"]["group"] == "tilt"
                    and e["case"]["kind"] in TEXT_KINDS
                    and e["case"]["tclass"] == tclass
                    and e["case"]["colour"] == colour
                ]
                print(_row(tool, rows))
    print("\n### per page kind (all tilts, both colours)\n")
    print(HDR.replace("| tool |", "| tool, kind |"))
    for tool in FULL + SKEW_ONLY:
        if tool not in res:
            continue
        for kind in TEXT_KINDS:
            rows = [e for e in res[tool].values() if e["case"]["group"] == "tilt" and e["case"]["kind"] == kind]
            print(_row(f"{tool}, {kind}", rows))


def _report_orientation(res: dict) -> None:
    print("\n## Orientation (groups tilt and turn, text kinds): page upright afterwards (within 45 deg)\n")
    print(
        "| tool | colour | tilt | cases | upright after | turned wrongly | left turned |\n|---|---|---|---|---|---|---|"
    )
    for tool in FULL + ORIENT_ONLY:
        if tool not in res:
            continue
        for colour in ("gray", "bw"):
            for tilts in ((0.0,), (-6.4,)):
                rows = [
                    e
                    for e in res[tool].values()
                    if e["case"]["group"] in ("tilt", "turn")
                    and e["case"]["kind"] in TEXT_KINDS
                    and e["case"]["colour"] == colour
                    and e["case"]["tilt"] in tilts
                ]
                n = len(rows)
                up = sum(e["upright"] for e in rows)
                wrong = sum(1 for e in rows if not e["upright"] and e["turned"])
                left = sum(1 for e in rows if not e["upright"] and not e["turned"])
                print(
                    f"| {tool} | {colour} | {tilts[0]:+.0f} | {n} | {_pct(up, n)} | {_pct(wrong, n)} | {_pct(left, n)} |"
                )
    print("\nby quarter turn (both colours, tilts 0 and -6.4):\n")
    print("| tool | turn 0 | 90 | 180 | 270 |\n|---|---|---|---|---|")
    for tool in FULL + ORIENT_ONLY:
        if tool not in res:
            continue
        cells = []
        for q in (0, 90, 180, 270):
            rows = [
                e
                for e in res[tool].values()
                if e["case"]["group"] in ("tilt", "turn")
                and e["case"]["kind"] in TEXT_KINDS
                and e["case"]["quarter"] == q
                and e["case"]["tilt"] in (0.0, -6.4)
            ]
            wrong = sum(1 for e in rows if not e["upright"] and e["turned"])
            cells.append(f"{sum(e['upright'] for e in rows)}/{len(rows)} ({wrong} wrong)")
        print(f"| {tool} | " + " | ".join(cells) + " |")


def _report_mirror(res: dict) -> None:
    print("\n## Mirrored pages, full grid: outcome counts per tool (lines level = best a rotation can do)\n")
    print("Each cell: lines level / left as scanned / better / worse, of the 8 cases (4 pages x gray, bw).\n")
    tools = [t for t in ORDER if t in res]
    print("| mirror, turn, tilt | " + " | ".join(tools) + " |\n|---|" + "---|" * len(tools))
    for m in ("H", "V"):
        for q in (0, 90, 180, 270):
            for tc in ("none", "small", "medium", "steep"):
                cells = []
                for tool in tools:
                    k = defaultdict(int)
                    for e in res[tool].values():
                        c = e["case"]
                        if c["group"] == "mirror" and c["mirror"] == m and c["quarter"] == q and c["tclass"] == tc:
                            k[e["outcome"]] += 1
                    cells.append(f"{k['right']}/{k['left']}/{k['better']}/{k['worse']}")
                print(f"| {m} {q} {tc} | " + " | ".join(cells) + " |")
    print("\nsummary per tool (all mirror cases):\n")
    print("| tool | cases | lines level | left | better | worse |\n|---|---|---|---|---|---|")
    for tool in res:
        rows = [e for e in res[tool].values() if e["case"]["group"] == "mirror"]
        k = defaultdict(int)
        for e in rows:
            k[e["outcome"]] += 1
        print(f"| {tool} | {len(rows)} | {k['right']} | {k['left']} | {k['better']} | {k['worse']} |")


def _report_artefacts(res: dict) -> None:
    print("\n## Scanner artefacts (4 text pages x 4 tilts x 2 colours each)\n")
    print("| tool | artefact | right | median / worst of the corrected | left | worse |\n|---|---|---|---|---|---|")
    for tool in FULL + SKEW_ONLY:
        if tool not in res:
            continue
        for a in ARTEFACTS:
            rows = [e for e in res[tool].values() if e["case"]["artefact"] == a]
            acted = [abs(e["after"]) for e in rows if e["acted"]]
            k = defaultdict(int)
            for e in rows:
                k[e["outcome"]] += 1
            ms = f"{statistics.median(acted):.2f} / {max(acted):.2f}" if acted else "-"
            print(f"| {tool} | {a} | {k['right']}/{len(rows)} | {ms} | {k['left']} | {k['worse']} |")


def _report_combo(res: dict) -> None:
    print("\n## Turned and tilted (group combo: see COMBOS; text kinds)\n")
    print("| tool | cases | right | upright | left | worse |\n|---|---|---|---|---|---|")
    for tool in FULL + ORIENT_ONLY + SKEW_ONLY:
        if tool not in res:
            continue
        rows = [e for e in res[tool].values() if e["case"]["group"] == "combo"]
        k = defaultdict(int)
        for e in rows:
            k[e["outcome"]] += 1
        print(
            f"| {tool} | {len(rows)} | {k['right']} | {sum(e['upright'] for e in rows)} | {k['left']} | {k['worse']} |"
        )


def _report_nontext(res: dict) -> None:
    print(
        "\n## Photo, little-text and nearly blank pages (own set: photo, little, blank; shared set: photo, nearly-blank)\n"
    )
    print(
        "| tool | kind | cases | right | left | better | worse | worse when straight and upright |\n|---|---|---|---|---|---|---|---|"
    )
    for tool in res:
        for kind in ("photo", "little", "blank", "nearly-blank"):
            rows = [
                e
                for e in res[tool].values()
                if e["case"]["group"] in ("tilt", "turn", "ocr-cond", "set2-tilt") and e["case"]["kind"] == kind
            ]
            k = defaultdict(int)
            for e in rows:
                k[e["outcome"]] += 1
            zero = [e for e in rows if e["case"]["quarter"] == 0 and e["case"]["tilt"] == 0.0]
            print(
                f"| {tool} | {kind} | {len(rows)} | {k['right']} | {k['left']} | {k['better']} | {k['worse']} |"
                f" {sum(e['outcome'] == 'worse' for e in zero)}/{len(zero)} |"
            )


ORDER = FULL + SKEW_ONLY + ORIENT_ONLY
KNOWN = ("tilt", "turn", "combo", "artefact", "ocr-cond", "set2-tilt")  # exact ground truth (mirror: separate)


def _cls(c: dict) -> str:
    return c["kind"] if c.get("set") == "ocr" else f"own:{c['kind']}"


def _report_classes(res: dict) -> None:
    """Per document class, every case with an exact answer (mirror grid apart): right (residual <= 0.5
    degrees) and made worse, as 'right% / worse%'. Skew-only tools are not run on turned pages."""
    tools = [t for t in ORDER if t in res]
    classes = sorted({_cls(e["case"]) for t in tools for e in res[t].values() if e["case"]["group"] in KNOWN})
    print("\n## Per document class: right (<= 0.5 deg) / made worse, all cases with an exact answer\n")
    print("| class | cases | " + " | ".join(tools) + " |\n|---|---|" + "---|" * len(tools))
    for cl in classes + ["ALL"]:
        cells, n = [], 0
        for t in tools:
            rows = [
                e for e in res[t].values() if e["case"]["group"] in KNOWN and (cl == "ALL" or _cls(e["case"]) == cl)
            ]
            n = max(n, len(rows))
            right = sum(e["outcome"] == "right" for e in rows)
            worse = sum(e["outcome"] == "worse" for e in rows)
            cells.append(f"{_pct(right, len(rows))} / {_pct(worse, len(rows))}" if rows else "-")
        print(f"| {cl} | {n} | " + " | ".join(cells) + " |")
    print("\n### the same, skew only: median / worst error of the corrected, and share left as scanned\n")
    tools = [t for t in FULL + SKEW_ONLY if t in res]
    print("| class | " + " | ".join(tools) + " |\n|---|" + "---|" * len(tools))
    for cl in classes + ["ALL"]:
        cells = []
        for t in tools:
            rows = [
                e
                for e in res[t].values()
                if e["case"]["group"] in KNOWN and e["case"]["quarter"] == 0 and (cl == "ALL" or _cls(e["case"]) == cl)
            ]
            acted = [abs(e["after"]) for e in rows if e["acted"]]
            left = sum(1 for e in rows if not e["acted"] and abs(e["before"]) > 0.5)
            ms = f"{statistics.median(acted):.2f} / {max(acted):.2f}" if acted else "- / -"
            cells.append(f"{ms}, left {_pct(left, len(rows))}" if rows else "-")
        print(f"| {cl} | " + " | ".join(cells) + " |")


def _report_real(res: dict) -> None:
    print("\n## Real pages (CORD receipts, FUNSD forms): own skew unknown\n")
    print(
        "Decided = the tool corrected the page as it is (angle or turn). Consistency = the residual of the same"
        " page with a known extra tilt (+4.7, -8.3) or turn (180, 90), relative to the tool's answer on the page"
        " as it is.\n"
    )
    print(
        "| tool | class | colour | pages | decided on the page as is | median own angle found | extra tilt: within 0.5"
        " | worst | turn: upright again |\n|---|---|---|---|---|---|---|---|---|"
    )
    for t in [t for t in ORDER if t in res]:
        for cl in REAL_CLASSES:
            for colour in ("gray", "bw"):
                rows = [e for e in res[t].values() if e["case"]["kind"] == cl and e["case"]["colour"] == colour]
                if not rows:
                    continue
                base = [e for e in rows if e["case"]["id"] == e["case"]["base"]]
                dec = sum(e["acted"] for e in base)
                own = [abs(e["raw"].get("angle") or 0) for e in base if e["acted"]]
                tilts = [e for e in rows if e["case"]["id"] != e["case"]["base"] and e["case"]["quarter"] == 0]
                ok = sum(abs(e["after"]) <= 0.5 for e in tilts)
                worst = f"{max(abs(e['after']) for e in tilts):.2f}" if tilts else "-"
                turns = [e for e in rows if e["case"]["quarter"]]
                up = sum(e["upright"] for e in turns)
                med = f"{statistics.median(own):.2f}" if own else "-"
                print(
                    f"| {t} | {cl} | {colour} | {len(base)} | {dec} | {med} | {ok}/{len(tilts)} | {worst} | {up}/{len(turns)} |"
                )


def _report_checks(res: dict) -> None:
    """The cases the OCR benchmark and the first look singled out."""
    if "tidyscan" not in res:
        return
    rs = res["tidyscan"]
    print("\n## Checks: tidyscan on the cases singled out earlier\n")
    for cid, e in sorted(rs.items()):
        c = e["case"]
        if c.get("cond") in ("bw-turn180-steep27", "gray-steep-33", "bw-crumpled-tilt12") or (
            c["kind"].startswith("book-curved") and c.get("cond") == "gray-tilt2.5"
        ):
            r = e["raw"]
            print(
                f"- {cid}: turn {r.get('turn')}, angle {r.get('angle')}, residual {e['after']:+.2f} ({e['outcome']}),"
                f" {r.get('seconds')} s"
            )
    print("\ntidyscan time by quarter turn (s: median / p90 / max, cases), text classes:\n")
    print("| turn | gray | bw |\n|---|---|---|")
    for q in (0, 90, 180, 270):
        cells = []
        for colour in ("gray", "bw"):
            s = sorted(
                e["seconds"]
                for e in rs.values()
                if e["case"]["quarter"] == q
                and e["case"]["colour"] == colour
                and (e["case"]["kind"] in TEXT_KINDS or e["case"]["kind"] in SET2_TEXT)
            )
            cells.append(
                f"{statistics.median(s):.1f} / {s[int(len(s) * 0.9)]:.1f} / {s[-1]:.1f} ({len(s)})" if s else "-"
            )
        print(f"| {q} | " + " | ".join(cells) + " |")
    print("\nslowest 15 tidyscan cases:\n")
    for e in sorted(rs.values(), key=lambda e: -e["seconds"])[:15]:
        print(f"- {e['case']['id']}: {e['seconds']:.1f} s, outcome {e['outcome']}")


def _report_time(res: dict) -> None:
    print("\n## Time per page (s, wall, one process, 1 thread for Tesseract)\n")
    print("| tool | median | p90 | max | errors |\n|---|---|---|---|---|")
    for tool in res:
        s = sorted(e["seconds"] for e in res[tool].values())
        err = sum(1 for e in res[tool].values() if e["error"])
        print(f"| {tool} | {statistics.median(s):.2f} | {s[int(len(s) * 0.9)]:.2f} | {s[-1]:.2f} | {err} |")


def _report_failures(res: dict) -> None:
    tool = "tidyscan"
    if tool not in res:
        return
    print("\n## tidyscan: every case made worse, or right orientation with skew error > 0.5\n")
    for e in sorted(res[tool].values(), key=lambda e: -abs(e["after"])):
        if e["outcome"] == "worse" or (e["acted"] and abs(e["after"]) > 0.5):
            r = e["raw"]
            print(
                f"- {e['case']['id']}: turn {r.get('turn')} angle {r.get('angle')} -> residual {e['after']:+.2f}"
                f" ({e['outcome']})"
            )


def main() -> None:
    cmd, work = sys.argv[1], sys.argv[2]
    if cmd == "fetch":
        fetch(work)
    elif cmd == "pages":
        pages(work)
    elif cmd == "cases":
        make_cases(work, sys.argv[sys.argv.index("--ocr-set") + 1] if "--ocr-set" in sys.argv else None)
    elif cmd == "run":
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 2
        for tool in sys.argv[3].split(","):
            run(work, tool, jobs)
    elif cmd == "probe":
        probe(work, sys.argv[3])
    elif cmd == "report":
        report(work)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
