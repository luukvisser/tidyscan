"""Measures orientation and skew detection on real page images (not part of the unit tests).

    python3 tests/eval_corpus.py PAGES_DIR      (needs Pillow and Tesseract with osd data)

PAGES_DIR holds page images at 300 dpi (PNG or JPEG). Each page is fed as a
gray JPEG and as black-and-white, turned to all four orientations, and
tilted by a set of small angles (and, as a receipt-sized copy on an A4 scan,
by up to 40 degrees, also upside down); the script reports how often the
orientation is right and how far the measured skew is off.

The pages used on 01-11-2026 come from OCRmyPDF's test resources and from
Rijksoverheid (see CORPUS); they are not in this repository because most are
CC-BY-SA. Fetch them, check the hashes, and render PDFs at 300 dpi gray
(e.g. pdftoppm -r 300 -gray -png).
"""

from __future__ import annotations

import io
import os
import statistics
import sys
import tempfile
import time

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from tidyscan import orientation, skew  # noqa: E402

BASE = "https://raw.githubusercontent.com/ocrmypdf/OCRmyPDF/main/tests/resources/"
CORPUS = {  # file: (url, sha256, licence)
    "linn.png": (
        BASE + "linn.png",
        "dcd90f08a6b8a733612f818330153636c13283dd548e2e092cad1fe628ec7bf3",
        "GFDL-1.2+ or CC-BY-SA-3.0",
    ),
    "typewriter.png": (
        BASE + "typewriter.png",
        "6f7a83685a83af954e9672b3e2db3253af165513d2826d780a48e175742f4469",
        "GFDL-1.2+ or CC-BY-SA",
    ),
    "c03-29.pdf": (
        BASE + "c03-29.pdf",
        "c2ff83af7d028c95209cc7eebdf80fd5bb4cd292973ed6601e4bab7d1929f201",
        "public domain",
    ),
    "multipage.pdf": (
        BASE + "multipage.pdf",
        "07987c44650938fa8dcf08c0937691712fdd800669b4607c2c7e3fee21cb1f80",
        "public domain",
    ),
    "epson.pdf": (
        BASE + "epson.pdf",
        "3fce2d6e5baffec839ffc86b7c91903b139399392ea76a09bdfcca40cb8b5a4d",
        "CC-BY-SA-3.0",
    ),
    "francais.pdf": (
        BASE + "francais.pdf",
        "600c27f8dd2ef085a94d3500f6ccdb3b37f4b89aa8ffdf9a00a06a1367887e8b",
        "CC-BY-SA-4.0",
    ),
    "lichtenstein.pdf": (
        BASE + "lichtenstein.pdf",
        "d7f2f944de30876e21b10caa465bc617e8fbd00285bdd1fc648d39793ebd76f8",
        "GFDL-1.2+ or CC-BY-SA-3.0",
    ),
    "rijksoverheid-publicaties.pdf": (
        "https://open.overheid.nl/repository/ronl-906efb5d895e5fb9cc1c28e198790a048077191f/1/pdf/overzicht-relevante-publicaties.pdf",
        "1eafc201d9347e470253cf663ce47d0c456bf4474ca30feedd3819524e3939b6",
        "CC0 (Rijksoverheid)",
    ),
}
TURNS = (0, 90, 180, 270)
TILTS = (-14.0, -3.1, -1.3, -0.5, 0.7, 1.8, 3.5, 8.0)
SMALL_TILTS = (-40.0, -27.0, -14.0, 8.0, 25.0, 33.0)


def as_page(img: Image.Image, kind: str) -> bytes:
    if kind == "bw":
        # a hard threshold as the printer does; Pillow's default convert("1") dithers text into noise
        bw = img.convert("L").point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)
        return b"P4\n%d %d\n" % ((bw.width + 7) // 8 * 8, bw.height) + bytes(b ^ 0xFF for b in bw.tobytes())
    out = io.BytesIO()
    img.convert("L").save(out, "JPEG", quality=90)
    return out.getvalue()


def main(pages_dir: str) -> None:
    work = tempfile.mkdtemp()
    orient_ok = orient_total = 0
    orient_none = 0
    skew_errors, skew_none = [], 0
    flipped = {"right": 0, "unsure": 0, "wrong": 0, "unsure skew": 0}
    started = time.monotonic()
    for name in sorted(os.listdir(pages_dir)):
        src = Image.open(os.path.join(pages_dir, name)).convert("L")
        line = []
        for kind in ("gray", "bw"):
            ok = 0
            for turn in TURNS:
                img = src.rotate(turn, expand=True)  # turned counter-clockwise by `turn`
                got = orientation.orientation(as_page(img, kind), work, 3.0, ("Latin",))
                orient_total += 1
                if got == turn:
                    ok += 1
                elif got == 0:
                    orient_none += 1  # kept as scanned: not wrong, only unhelped
            orient_ok += ok
            errs = []
            for tilt in TILTS:
                d = skew.detect(as_page(src.rotate(tilt, resample=Image.Resampling.BICUBIC, fillcolor=255), kind), 300)
                if d is None:
                    skew_none += 1
                    continue
                errs.append(abs(d + tilt))
            skew_errors += errs
            worst = f"{max(errs):.2f}" if errs else "-"
            line.append(f"{kind}: orientation {ok}/4, skew worst {worst} ({len(TILTS) - len(errs)} unsure)")
            # a small document (receipt-like: a third of the width) near the top-left of an empty A4 scan
            small = src.resize((src.width // 3, src.height // 3), Image.Resampling.BOX)
            # up to 40 degrees: a receipt lies anywhere on the glass (01-11-2026: 25 and 30 degrees, live)
            for tilt in SMALL_TILTS:
                a4 = Image.new("L", (2480, 3508), 255)
                a4.paste(small.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255), (150, 150))
                d = skew.detect(as_page(a4, kind), 300)
                if d is None:
                    skew_none += 1
                else:
                    skew_errors.append(abs(d + tilt))
                    if abs(d + tilt) > 0.3:
                        line.append(f"small {tilt:+} -> {d}")
            # upside down and tilted: skew first, then the orientation of the straightened page
            for tilt in (-27.0, 31.0):
                a4 = Image.new("L", (2480, 3508), 255)
                turned = small.rotate(180 + tilt, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
                a4.paste(turned, (150, 150))
                page = as_page(a4, kind)
                d = skew.detect(page, 300)
                if d is None:
                    skew_none += 1
                    flipped["unsure skew"] += 1
                    continue
                skew_errors.append(abs(d + tilt))
                got = orientation.orientation(page, work, 3.0, ("Latin",), skew=d)
                flipped["right" if got == 180 else "unsure" if got == 0 else "wrong"] += 1
        print(f"{name:32s} " + "; ".join(line), flush=True)
    print()
    print(
        f"orientation: {orient_ok}/{orient_total} right, {orient_none} kept as scanned (unsure), "
        f"{orient_total - orient_ok - orient_none} wrong"
    )
    print(f"upside down and tilted, orientation after straightening: {flipped}")
    if skew_errors:
        print(
            f"skew: {len(skew_errors)} measured, {skew_none} unsure; error max {max(skew_errors):.2f}, "
            f"median {statistics.median(skew_errors):.2f}, over 0.3: {sum(e > 0.3 for e in skew_errors)}"
        )
    print(f"{time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main(sys.argv[1])
