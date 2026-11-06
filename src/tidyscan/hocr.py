"""hOCR from text boxes: what OCRmyPDF turns into the invisible text layer of a page.

An OCR engine that finds text boxes (PaddleOCR's detector finds one box per run of text, often
several per printed line) gives Line objects; page() groups the boxes into rows in reading order
and writes the hOCR that OCRmyPDF's renderer reads: ocr_page > ocr_carea > ocr_par > ocr_line >
ocrx_word, each with a pixel bbox. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels, y down


@dataclass
class Word:
    text: str
    box: Box
    conf: float = 1.0  # 0..1


@dataclass
class Line:
    text: str
    box: Box
    conf: float = 1.0
    words: list[Word] = field(default_factory=list)


# Characters XML 1.0 does not allow (OCRmyPDF parses the hOCR as XML).
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def clean(text: str) -> str:
    return _NOT_XML.sub("", text)


def bbox(points: Sequence[Sequence[float]], width: int, height: int) -> Box:
    """The axis-aligned box around points [(x, y), ...] (or an n x 2 array), clipped to the page."""
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
    x1, y1 = min(width, int(round(max(xs)))), min(height, int(round(max(ys))))
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def split_words(text: str, box: Box, conf: float = 1.0) -> list[Word]:
    """Words of a line with boxes in proportion to their character positions in it."""
    x0, y0, x1, y1 = box
    per_char = (x1 - x0) / max(1, len(text))
    return [
        Word(
            m.group(),
            (
                x0 + int(m.start() * per_char),
                y0,
                max(x0 + int(m.start() * per_char) + 1, x0 + int(round(m.end() * per_char))),
                y1,
            ),
            conf,
        )
        for m in re.finditer(r"\S+", text)
    ]


def rows(lines: list[Line]) -> list[list[Line]]:
    """Lines grouped into rows (boxes that overlap vertically by half the smaller height), top to
    bottom, each row left to right."""
    out: list[list[Line]] = []
    for line in sorted(lines, key=lambda ln: (ln.box[1] + ln.box[3]) / 2):
        if out:
            row = out[-1]
            top, bottom = min(ln.box[1] for ln in row), max(ln.box[3] for ln in row)
            overlap = min(bottom, line.box[3]) - max(top, line.box[1])
            if overlap > 0.5 * min(bottom - top, line.box[3] - line.box[1]):
                row.append(line)
                continue
        out.append([line])
    return [sorted(row, key=lambda ln: ln.box[0]) for row in out]


def _union(boxes: Iterable[Box]) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _title(box: Box, extra: str = "") -> str:
    return quoteattr(f"bbox {box[0]} {box[1]} {box[2]} {box[3]}{extra}")


def page(width: int, height: int, lines: list[Line], lang: str = "", system: str = "tidyscan") -> tuple[str, str]:
    """(hOCR document, plain text) of one page image of width x height pixels.

    A row of boxes becomes one ocr_line, so OCRmyPDF puts spaces between its words and the text
    reads row by row. Lines and words without text are left out."""
    kept = []
    for line in lines:
        words = [Word(clean(w.text).strip(), w.box, w.conf) for w in line.words] or split_words(
            clean(line.text), line.box, line.conf
        )
        words = [w for w in words if w.text]
        if words:
            kept.append(Line(" ".join(w.text for w in words), line.box, line.conf, words))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"'
        ' "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">',
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">',
        '<head><title></title><meta http-equiv="Content-Type" content="text/html;charset=utf-8"/>',
        f'<meta name="ocr-system" content={quoteattr(system)}/>',
        '<meta name="ocr-capabilities" content="ocr_page ocr_carea ocr_par ocr_line ocrx_word"/></head>',
        "<body>",
        f'<div class="ocr_page" id="page_1" title={quoteattr(f"bbox 0 0 {width} {height}; ppageno 0")}>',
    ]
    text = []
    lang_attr = f" lang={quoteattr(lang)}" if lang else ""
    n_word = 0
    for n, row in enumerate(rows(kept), 1):
        box = _union(ln.box for ln in row)
        h = box[3] - box[1]
        # Baseline a fifth of the box height above its bottom: the detector's boxes have room for
        # descenders. It only moves where a viewer draws the selection, not what is found.
        out.append(f'<div class="ocr_carea" id="block_1_{n}" title={_title(box)}>')
        out.append(f'<p class="ocr_par" id="par_1_{n}"{lang_attr} title={_title(box)}>')
        out.append(
            f'<span class="ocr_line" id="line_1_{n}" title={_title(box, f"; baseline 0 -{h // 5}; x_size {h}")}>'
        )
        for line in row:
            for w in line.words:
                n_word += 1
                conf = max(0, min(100, round(w.conf * 100)))
                out.append(
                    f'<span class="ocrx_word" id="word_1_{n_word}" title={_title(w.box, f"; x_wconf {conf}")}>'
                    f"{escape(w.text)}</span>"
                )
        out.append("</span></p></div>")
        text.append(" ".join(ln.text for ln in row))
    out += ["</div>", "</body>", "</html>", ""]
    return "\n".join(out), "\n".join(text) + ("\n" if text else "")
