from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from typing import TYPE_CHECKING, Any, ClassVar, Never, TypeAlias
from unittest import mock

import tidyscan
from tidyscan import __main__ as cli
from tidyscan import hocr, models, ocr, orientation, paddle, pdf, skew

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Sequence
    from xml.etree.ElementTree import Element

    from ocrmypdf.models.ocr_element import OcrElement
    from PIL import Image


def fake_jpeg(width: int = 40, height: int = 30, comps: int = 1, size: int = 2000) -> bytes:
    """Headers a JPEG reader accepts for its size; the body is not image data."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * comps, 8, height, width, comps) + b"\x01\x11\x00" * comps
    return b"\xff\xd8" + sof + b"\xff\xda\x00\x02" + bytes((i * 7) % 251 for i in range(size)) + b"\xff\xd9"


def fake_pbm(width: int = 64, height: int = 40) -> bytes:
    return b"P4\n%d %d\n" % (width, height) + bytes((i * 13) % 256 for i in range(width // 8 * height))


@contextlib.contextmanager
def fake_bin(name: str, script: str) -> Generator[None]:
    with tempfile.TemporaryDirectory() as bindir:
        path = os.path.join(bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + script)
        os.chmod(path, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            yield


class PdfTest(unittest.TestCase):
    def test_structure(self) -> None:
        out = pdf.build([fake_jpeg(comps=1), fake_jpeg(comps=3), fake_pbm()], 300)
        self.assertTrue(out.startswith(b"%PDF-1.4") and out.rstrip().endswith(b"%%EOF"))
        found = re.search(rb"startxref\n(\d+)", out)
        assert found
        xref = int(found.group(1))
        self.assertTrue(out[xref:].startswith(b"xref"))
        offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", out)]
        for n, off in enumerate(offsets, 1):
            self.assertTrue(out[off:].startswith(b"%d 0 obj" % n))
        self.assertIn(b"/Count 3", out)
        self.assertIn(b"/DeviceGray", out)
        self.assertIn(b"/DeviceRGB", out)
        self.assertIn(b"/BitsPerComponent 1 /Decode [1 0]", out)

    def test_unsupported(self) -> None:
        for page in (b"hello", b"P4\n8 8\n\x00"):
            with self.assertRaises(pdf.UnsupportedPage):
                pdf.build([page], 300)

    def test_rotation(self) -> None:
        out = pdf.build([fake_jpeg(), fake_jpeg()], 300, [90, 0])
        self.assertEqual(re.findall(rb"/Rotate (\d+)", out), [b"90", b"0"])
        with self.assertRaises(ValueError):
            pdf.build([fake_jpeg()], 300, [45])

    def test_skew_draws_the_scan_turned(self) -> None:
        out = pdf.build([fake_jpeg()], 300, [0], [1.5])
        m = re.search(rb"(-?[\d.]+) (-?[\d.]+) (-?[\d.]+) (-?[\d.]+) 0 0 cm [\d.]+ 0 0 [\d.]+ -", out)
        assert m
        c, s_, ms, c2 = (float(x) for x in m.groups())
        self.assertAlmostEqual(c, 0.999657, places=5)
        self.assertAlmostEqual(s_, 0.026177, places=5)
        self.assertEqual((ms, c2), (-s_, c))
        with self.assertRaises(ValueError):
            pdf.build([fake_jpeg()], 300, [0], [60])


class OrientationTest(unittest.TestCase):
    def test_threshold_and_failures(self) -> None:
        cases = [
            (
                "Page number: 0\nOrientation in degrees: 270\nRotate: 90\nOrientation confidence: 5.81\n"
                "Script: Latin\nScript confidence: 2.1\n",
                0,
                90,
            ),
            ("Rotate: 180\nOrientation confidence: 6.68\nScript: Latin\n", 0, 180),
            ("Rotate: 180\nOrientation confidence: 3.59\nScript: Cyrillic\n", 0, 0),  # the corpus case
            ("Rotate: 180\nOrientation confidence: 6.68\n", 0, 0),  # no script: unsure
            ("Rotate: 90\nOrientation confidence: 1.2\n", 0, 0),  # below the threshold
            ("Rotate: 0\nOrientation confidence: 1.89\n", 0, 0),
            ("Too few characters. Skipping this page\n", 1, 0),
            ("Rotate: 45\nOrientation confidence: 9\n", 0, 0),
        ]
        for page in (fake_jpeg(), fake_pbm()):
            for out, code, want in cases:
                with tempfile.TemporaryDirectory() as tmp, fake_bin("tesseract", f"printf '%s' '{out}'\nexit {code}\n"):
                    self.assertEqual(orientation.orientation(page, tmp, 3.0), want, out)
                    self.assertEqual(os.listdir(tmp), [])

    def test_sure_or_not(self) -> None:
        cases = [
            ("Rotate: 90\nOrientation confidence: 5.81\nScript: Latin\n", (90, True)),
            ("Rotate: 0\nOrientation confidence: 4.2\nScript: Latin\n", (0, True)),
            ("Rotate: 0\nOrientation confidence: 1.89\nScript: Latin\n", (0, False)),  # upright, but unsure
            ("Rotate: 0\nOrientation confidence: 6\nScript: Cyrillic\n", (0, False)),
            ("Rotate: 90\nOrientation confidence: 1.2\nScript: Latin\n", (0, False)),  # kept: not sure
            ("Too few characters. Skipping this page\n", (0, False)),
        ]
        for out, want in cases:
            with tempfile.TemporaryDirectory() as tmp, fake_bin("tesseract", f"printf '%s' '{out}'\n"):
                self.assertEqual(orientation.detect(fake_jpeg(), tmp, 3.0), want, out)

    def test_tesseract_input_keeps_its_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_bin("tesseract", f'cp "$1" {tmp}/seen\n'):
            for page, start in ((fake_jpeg(), b"\xff\xd8"), (fake_pbm(), b"P4")):
                orientation.orientation(page, tmp)
                with open(os.path.join(tmp, "seen"), "rb") as f:
                    self.assertTrue(f.read().startswith(start))


def blank() -> Image.Image:
    from PIL import Image

    return Image.new("L", (200, 280), 255)


def blank_jpeg() -> bytes:
    out = io.BytesIO()
    blank().save(out, "JPEG", dpi=(300, 300))
    return out.getvalue()


class StraightenTest(unittest.TestCase):
    def test_skew_and_turn(self) -> None:
        with (
            mock.patch("tidyscan.skew.detect", return_value=1.2) as d,
            mock.patch("tidyscan.orientation.detect", return_value=(90, True)) as o,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (90, 1.2))
        self.assertEqual(o.call_args.kwargs["skew"], 1.2)
        self.assertEqual([c.args[2] for c in d.call_args_list], [0, 90])  # measured again upright

    def test_steep_page_is_measured_a_quarter_turn_on(self) -> None:
        # unsure at 0 (rows beyond the +-45 search), found at 90; OSD then says upside down
        with (
            mock.patch(
                "tidyscan.skew.detect", side_effect=lambda page, dpi, turn, wd: None if turn == 0 else -28.0
            ) as d,
            mock.patch("tidyscan.orientation.detect", return_value=(180, True)) as o,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (270, -28.0))
        self.assertEqual([c.args[2] for c in d.call_args_list], [0, 90])
        self.assertEqual(o.call_args.kwargs["quarter_turn"], 90)
        self.assertEqual(o.call_args.kwargs["skew"], -28.0)

    def test_steep_quarter_turn_needs_a_sure_orientation(self) -> None:
        # the benchmark's 10 wrong turns: OSD unsure on the turned page, the 90 kept anyway
        steep = lambda page, dpi, turn, wd: None if turn == 0 else -6.4  # noqa: E731
        with (
            mock.patch("tidyscan.skew.detect", side_effect=steep),
            mock.patch("tidyscan.orientation.detect", return_value=(0, False)),
            mock.patch("tidyscan.orientation.quarter_by_words", return_value=None),
            self.assertLogs("tidyscan", "INFO") as logs,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (0, -6.4))  # sideways, but level
        self.assertIn("quarter turn not confirmed", "\n".join(logs.output))
        with (
            mock.patch("tidyscan.skew.detect", side_effect=steep),
            mock.patch("tidyscan.orientation.detect", return_value=(0, True)),
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (90, -6.4))  # confirmed upright
        with (
            mock.patch("tidyscan.skew.detect", side_effect=steep),
            mock.patch("tidyscan.orientation.detect", return_value=(0, False)),
            mock.patch("tidyscan.orientation.quarter_by_words", return_value=270) as w,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (270, -6.4))  # the words settle it
        self.assertEqual(w.call_args.kwargs["skew"], -6.4)

    @unittest.skipUnless(skew.available(), "needs tesseract and Pillow")
    def test_quarter_by_words(self) -> None:
        with mock.patch("tidyscan.orientation._word_score", side_effect=[175, 19]):
            self.assertEqual(orientation.quarter_by_words(blank_jpeg(), skew=-6.4), 90)
        with mock.patch("tidyscan.orientation._word_score", side_effect=[19, 175]):
            self.assertEqual(orientation.quarter_by_words(blank_jpeg()), 270)
        for sides in ([0, 0], [40, 20], [9, 0]):  # none, not 3 times more, too few words
            with mock.patch("tidyscan.orientation._word_score", side_effect=sides):
                self.assertIsNone(orientation.quarter_by_words(blank_jpeg()), sides)

    def test_steep_answer_near_45_is_refused(self) -> None:
        # the chart with slanted axis labels: a quarter turn plus about 45 degrees
        with (
            mock.patch("tidyscan.skew.detect", side_effect=lambda page, dpi, turn, wd: None if turn == 0 else 44.1),
            mock.patch("tidyscan.orientation.detect", return_value=(0, True)) as o,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (0, 0.0))
        self.assertEqual(o.call_args.kwargs["quarter_turn"], 0)

    def test_steep_path_only_when_unsure_and_turning_allowed(self) -> None:
        with (
            mock.patch("tidyscan.skew.detect", return_value=None) as d,
            mock.patch("tidyscan.orientation.detect", return_value=(0, False)),
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300, rotate=False), (0, 0.0))
            self.assertEqual([c.args[2] for c in d.call_args_list], [0])
            d.reset_mock()
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300), (0, 0.0))  # unsure both ways
            self.assertEqual([c.args[2] for c in d.call_args_list], [0, 90])

    def test_known_turn_small_skew_and_off(self) -> None:
        with (
            mock.patch("tidyscan.skew.detect", return_value=0.2),
            mock.patch("tidyscan.orientation.detect") as o,
        ):
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300, turn=180), (180, 0.0))
            o.assert_not_called()
            self.assertEqual(tidyscan.straighten(fake_jpeg(), 300, rotate=False, deskew=False), (0, 0.0))

    def test_never_raises(self) -> None:
        with (
            mock.patch("tidyscan.skew.detect", side_effect=OSError("x")),
            mock.patch("tidyscan.orientation.detect", side_effect=ValueError("y")),
            self.assertLogs("tidyscan", "WARNING"),
        ):
            self.assertEqual(tidyscan.straighten(b"garbage", 300), (0, 0.0))

    @unittest.skipUnless(skew.available(), "needs tesseract and Pillow")
    def test_a_just_missed_check_is_measured_once_more(self) -> None:
        # -27.63 with +0.52 left was kept as scanned (benchmark 05-11-2026, weak spot 3)
        with (
            mock.patch("tidyscan.skew._base", side_effect=lambda *a: blank()),
            mock.patch("tidyscan.skew.coarse_peaks", return_value=[-28.0]),
            mock.patch(
                "tidyscan.skew._measure", side_effect=[None, (0.5, 0.1, 9), (0.52, 0.1, 9), (0.05, 0.1, 9)]
            ) as m,
        ):
            self.assertEqual(skew.detect(fake_jpeg(), 300, 0), -26.93)  # -28 + 0.5 + 0.52 + 0.05
        self.assertEqual(m.call_count, 4)
        with (
            mock.patch("tidyscan.skew._base", side_effect=lambda *a: blank()),
            mock.patch("tidyscan.skew.coarse_peaks", return_value=[]),
            mock.patch(
                "tidyscan.skew._measure", side_effect=[(1.0, 0.1, 9), (0.8, 0.1, 9), (0.7, 0.1, 9)] + [None] * 4
            ),
        ):
            self.assertIsNone(skew.detect(fake_jpeg(), 300, 0))  # the third look still off: unsure

    @unittest.skipUnless(skew.available(), "needs tesseract and Pillow")
    def test_sideways_lines_end_the_search(self) -> None:
        # a page turned a quarter: its lines measure near 90; no 300-dpi pass, no other candidates
        with (
            mock.patch("tidyscan.skew._base", side_effect=lambda *a: blank()),
            mock.patch("tidyscan.skew.coarse_peaks", return_value=[0.0, 30.0]),
            mock.patch("tidyscan.skew._measure", return_value=(89.9, 0.1, 9)) as m,
        ):
            self.assertIsNone(skew.detect(fake_jpeg(), 300, 0))
        self.assertEqual(m.call_count, 1)

    @unittest.skipUnless(skew.available(), "needs tesseract and Pillow")
    def test_detects_a_known_angle(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        page = Image.new("L", (2480, 3437), 255)
        d = ImageDraw.Draw(page)
        font = ImageFont.load_default(size=40)
        text = "The quick brown fox jumps over the lazy dog, and then reads a long letter."
        for n, y in enumerate(range(300, 3000, 70)):
            d.text((200, y), f"{n:02d} {text}", fill=0, font=font)
        with tempfile.TemporaryDirectory() as tmp:
            for applied in (0.0, 1.0, -2.2, 3.5):
                im = page.rotate(applied, resample=Image.Resampling.BICUBIC, fillcolor=255).convert("1")
                pbm = b"P4\n%d %d\n" % ((im.width + 7) // 8 * 8, im.height) + bytes(x ^ 0xFF for x in im.tobytes())
                got = skew.detect(pbm, 300, 0, tmp)
                self.assertIsNotNone(got, msg=applied)
                assert got is not None
                self.assertAlmostEqual(got, -applied, delta=0.2, msg=applied)
            self.assertIsNone(skew.detect(b"P4\n2480 3437\n" + bytes(310 * 3437), 300, 0, tmp))  # blank
            self.assertEqual(os.listdir(tmp), [])


class OcrTest(unittest.TestCase):
    # the fake copies its input and appends the languages; arguments: ... -l LANGS SRC DST
    COPY = 'for a; do last2="$last"; last="$a"; done\ncat "$last2" > "$last"; printf "%%OCR-%s" "$5" >> "$last"\n'

    def test_applied_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_bin("ocrmypdf", self.COPY):
            out = ocr.add_text_layer(b"%PDF-plain", "nld+eng", tmp)
            self.assertEqual(out, b"%PDF-plain%OCR-nld+eng")
            self.assertEqual(os.listdir(tmp), [])

    def test_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_bin("ocrmypdf", "echo broken >&2; exit 3\n"):
            with self.assertRaisesRegex(ocr.OcrError, "exit 3: broken"):
                ocr.add_text_layer(b"%PDF-plain", "eng", tmp)
            self.assertEqual(os.listdir(tmp), [])


class HocrTest(unittest.TestCase):
    def parse(self, doc: str) -> tuple[Element, dict[str, str]]:
        from xml.etree import ElementTree

        root = ElementTree.fromstring(doc)
        ns = {"h": "http://www.w3.org/1999/xhtml"}
        return root, ns

    def test_structure_boxes_and_order(self) -> None:
        lines = [
            hocr.Line("12,99", (800, 105, 900, 140), 0.9),  # right part of row 1
            hocr.Line("second row", (100, 300, 400, 340), 0.8),
            hocr.Line(
                "Total bread",
                (100, 100, 420, 145),
                0.95,
                [hocr.Word("Total", (100, 100, 250, 145), 0.99), hocr.Word("bread", (270, 100, 420, 145), 0.9)],
            ),
        ]
        doc, text = hocr.page(1000, 500, lines)
        self.assertEqual(text, "Total bread 12,99\nsecond row\n")
        root, ns = self.parse(doc)
        page = root.find(".//h:div[@class='ocr_page']", ns)
        assert page is not None
        self.assertIn("bbox 0 0 1000 500", page.get("title", ""))
        rows = root.findall(".//h:p[@class='ocr_par']/h:span[@class='ocr_line']", ns)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].get("title", "").startswith("bbox 100 100 900 145; baseline 0 -9"))
        words = [(w.text, w.get("title")) for w in rows[0].findall("h:span[@class='ocrx_word']", ns)]
        self.assertEqual(
            words,
            [
                ("Total", "bbox 100 100 250 145; x_wconf 99"),
                ("bread", "bbox 270 100 420 145; x_wconf 90"),
                ("12,99", "bbox 800 105 900 140; x_wconf 90"),
            ],
        )
        # a line without word boxes is split by character position: "second" 6 of 10 characters
        second = [w.get("title") for w in rows[1].findall("h:span[@class='ocrx_word']", ns)]
        self.assertEqual(second, ["bbox 100 300 280 340; x_wconf 80", "bbox 310 300 400 340; x_wconf 80"])

    def test_escaping_and_empty(self) -> None:
        doc, text = hocr.page(
            100, 50, [hocr.Line('a<b & "c"\x00\x07', (1, 1, 90, 20)), hocr.Line("   ", (1, 30, 90, 45))], lang="nld"
        )
        root, ns = self.parse(doc)  # well-formed despite < & " and controls
        self.assertEqual(
            [w.text for w in root.iter("{http://www.w3.org/1999/xhtml}span") if w.get("class") == "ocrx_word"],
            ["a<b", "&", '"c"'],
        )
        par = root.find(".//h:p", ns)
        assert par is not None
        self.assertEqual(par.get("lang"), "nld")
        self.assertEqual(text, 'a<b & "c"\n')
        self.assertEqual(hocr.page(100, 50, [])[1], "")

    def test_bbox_clips_to_the_page(self) -> None:
        self.assertEqual(hocr.bbox([(-3, 5.4), (120.6, 5), (121, 60), (-2, 61)], 100, 50), (0, 5, 100, 50))
        self.assertEqual(hocr.bbox([(10, 10)] * 4, 100, 50), (10, 10, 11, 11))  # never empty

    @unittest.skipUnless(importlib.util.find_spec("ocrmypdf"), "needs ocrmypdf")
    def test_ocrmypdf_renders_it(self) -> None:
        import ocrmypdf.hocrtransform as ht

        if not hasattr(ht, "HocrTransform"):
            self.skipTest("OCRmyPDF 17 has no HocrTransform; test_ocrmypdf_parses_it covers it")
        doc, _ = hocr.page(2480, 3508, [hocr.Line("Hello <world>", (300, 300, 1200, 380), 0.9)])
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "p.hocr"), os.path.join(tmp, "p.pdf")
            with open(src, "w", encoding="utf-8") as f:
                f.write(doc)
            ht.HocrTransform(hocr_filename=src, dpi=300).to_pdf(out_filename=dst)
            with open(dst, "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF"))

    @unittest.skipUnless(importlib.util.find_spec("ocrmypdf"), "needs ocrmypdf")
    def test_ocrmypdf_parses_it(self) -> None:
        """OCRmyPDF 17 reads hOCR into OcrElements for fpdf2: the row stays one line, words keep their boxes."""
        import ocrmypdf.hocrtransform as ht

        if not hasattr(ht, "HocrParser"):
            self.skipTest("OCRmyPDF 16 renders hOCR itself; test_ocrmypdf_renders_it covers it")
        lines = [hocr.Line("Milk", (300, 300, 500, 380), 0.9), hocr.Line("1.29", (1800, 305, 1950, 382), 0.8)]
        doc, _ = hocr.page(2480, 3508, lines)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "p.hocr")
            with open(src, "w", encoding="utf-8") as f:
                f.write(doc)
            page = ht.HocrParser(src).parse()
        found = []

        def left(word: OcrElement) -> int:
            assert word.bbox is not None
            return round(word.bbox.left)

        def walk(el: OcrElement) -> None:
            if el.ocr_class == "ocr_line":
                found.append([(w.text, left(w)) for w in el.children if w.ocr_class == "ocrx_word"])
            for child in el.children:
                walk(child)

        walk(page)
        self.assertEqual(found, [[("Milk", 300), ("1.29", 1800)]])


Words: TypeAlias = "Sequence[Sequence[tuple[str, float, Sequence[Sequence[float]] | None]]]"


class FakeResult:
    """A RapidOCR result (paddle.RapidOcrResult)."""

    def __init__(
        self,
        boxes: Sequence[Sequence[Sequence[float]]] | None,
        txts: Sequence[str] | None,
        scores: Sequence[float] | None,
        word_results: Words | None = (),
    ) -> None:
        self.boxes, self.txts, self.scores, self.word_results = boxes, txts, scores, word_results


class PaddleLinesTest(unittest.TestCase):
    QUAD = [[10, 10], [110, 10], [110, 30], [10, 30]]

    def test_word_boxes_used_when_they_match(self) -> None:
        r = FakeResult(
            [self.QUAD],
            ("ab cd",),
            (0.9,),
            (
                (
                    ("ab", 0.8, [[10, 10], [40, 10], [40, 30], [10, 30]]),
                    ("cd", 0.7, [[60, 10], [110, 10], [110, 30], [60, 30]]),
                ),
            ),
        )
        (line,) = paddle.lines(r, 200, 100)
        self.assertEqual(line.box, (10, 10, 110, 30))
        self.assertEqual([(w.text, w.box) for w in line.words], [("ab", (10, 10, 40, 30)), ("cd", (60, 10, 110, 30))])

    def test_misaligned_word_boxes_ignored(self) -> None:
        two = [self.QUAD, [[10, 50], [110, 50], [110, 70], [10, 70]]]
        word = (("xy", 0.8, [[10, 10], [40, 10], [40, 30], [10, 30]]),)
        r = FakeResult(two, ("ab", "cd"), (0.9, 0.9), (word,))  # one word list for two lines
        self.assertEqual([ln.words for ln in paddle.lines(r, 200, 100)], [[], []])
        r = FakeResult([self.QUAD], ("ab",), (0.9,), (word,))  # other characters
        self.assertEqual(paddle.lines(r, 200, 100)[0].words, [])
        self.assertEqual(paddle.lines(FakeResult(None, None, None), 200, 100), [])

    def test_languages(self) -> None:
        self.assertTrue(paddle.covers("nld+eng"))
        self.assertFalse(paddle.covers("eng+jpn"))

    def test_threads(self) -> None:
        with mock.patch.dict(os.environ, {paddle.THREADS_ENV: "3"}):
            self.assertEqual(paddle.threads_wanted(), 3)
        with (
            mock.patch.dict(os.environ, {paddle.THREADS_ENV: ""}),
            mock.patch("os.sched_getaffinity", return_value={0, 1, 2, 3}),
        ):
            for cpu_max, want in (
                ("200000 100000\n", 2),
                ("150000 100000\n", 2),
                ("max 100000\n", None),
                ("900000 100000\n", 4),
            ):
                with mock.patch("builtins.open", mock.mock_open(read_data=cpu_max)):
                    self.assertEqual(paddle.threads_wanted(), want, cpu_max)
            with mock.patch("builtins.open", side_effect=OSError):
                self.assertIsNone(paddle.threads_wanted())

    def test_missing_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {paddle.MODELS_ENV: tmp}):
            self.assertIn(os.path.join(tmp, paddle.DET), paddle.missing())


class EngineChoiceTest(unittest.TestCase):
    # records each call's arguments (one line per call), copies when asked; fails Paddle runs if $FAIL_PADDLE
    RECORD = (
        'echo "$*" >> "$OCR_CALLS"\n'
        'case "$*" in *--jobs*) [ -n "$FAIL_PADDLE" ] && { echo paddle-broke >&2; exit 2; };; esac\n'
        'for a; do last2="$last"; last="$a"; done\ncat "$last2" > "$last"\n'
    )

    def run_ocr(
        self,
        engine: str = "auto",
        languages: str = "eng",
        lacking: Iterable[str] = (),
        fail_paddle: bool = False,
        major: int = 16,
        installed: bool = False,
    ) -> None:
        with (
            mock.patch("tidyscan.ocr._ocrmypdf_major", return_value=major),
            mock.patch("tidyscan.ocr.plugin_installed", return_value=installed),
            tempfile.TemporaryDirectory() as tmp,
            fake_bin("ocrmypdf", self.RECORD),
            mock.patch.dict(
                os.environ, {"OCR_CALLS": os.path.join(tmp, "calls"), "FAIL_PADDLE": "1" if fail_paddle else ""}
            ),
            mock.patch("tidyscan.paddle.missing", return_value=list(lacking)),
        ):
            work = os.path.join(tmp, "w")
            os.mkdir(work)
            try:
                out = ocr.add_text_layer(b"%PDF-x", languages, work, engine=engine)
                self.assertEqual(out, b"%PDF-x")
            finally:
                with open(os.path.join(tmp, "calls")) as f:
                    self.calls = [line.split() for line in f]
                self.assertEqual(os.listdir(work), [])

    def test_auto_takes_paddle_when_available(self) -> None:
        self.run_ocr()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:7], ["--quiet", "--output-type", "pdf", "-l", "eng", "--plugin", ocr.PLUGIN])
        self.assertIn("--jobs", self.calls[0])

    def test_tesseract_and_fallbacks(self) -> None:
        self.run_ocr("tesseract")
        self.assertEqual([c[:5] + c[5:-2] for c in self.calls], [["--quiet", "--output-type", "pdf", "-l", "eng"]])
        self.run_ocr("auto", lacking=["rapidocr"])  # quietly Tesseract
        self.assertNotIn("--plugin", self.calls[0])
        with self.assertLogs("tidyscan.ocr", "WARNING"):
            self.run_ocr("paddle", lacking=["rapidocr"])
        self.assertNotIn("--plugin", self.calls[0])
        self.run_ocr("auto", languages="eng+jpn")  # not a Latin-script language
        self.assertNotIn("--plugin", self.calls[0])

    def test_pdfa(self) -> None:
        with (
            mock.patch("tidyscan.ocr._ocrmypdf_major", return_value=16),
            mock.patch("tidyscan.ocr.plugin_installed", return_value=False),
            tempfile.TemporaryDirectory() as tmp,
            fake_bin("ocrmypdf", self.RECORD),
            mock.patch.dict(os.environ, {"OCR_CALLS": os.path.join(tmp, "calls"), "FAIL_PADDLE": ""}),
        ):
            ocr.add_text_layer(b"%PDF-x", "eng", tmp, engine="tesseract", pdfa=True)
            with open(os.path.join(tmp, "calls")) as f:
                self.assertEqual(f.read().split()[:3], ["--quiet", "--output-type", "pdfa-2"])

    def test_ocrmypdf_17_names_the_engine(self) -> None:
        self.run_ocr(major=17)
        self.assertEqual(self.calls[0][-4:-2], ["--ocr-engine", "paddle"])
        self.assertIn("--plugin", self.calls[0])
        self.run_ocr("tesseract", major=17)
        self.assertEqual(self.calls[0][-4:-2], ["--ocr-engine", "tesseract"])

    def test_an_installed_plugin_is_not_loaded_twice(self) -> None:
        # a package with the entry point makes OCRmyPDF load it; a second --plugin would stop OCRmyPDF
        self.run_ocr(major=17, installed=True)
        self.assertNotIn("--plugin", self.calls[0])
        self.assertIn("paddle", self.calls[0])
        with self.assertLogs("tidyscan.ocr", "WARNING"):
            self.run_ocr(major=17, installed=True, fail_paddle=True)
        self.assertEqual(self.calls[1][-4:-2], ["--ocr-engine", "tesseract"])  # Tesseract asked for by name
        with self.assertLogs("tidyscan.ocr", "WARNING") as logs:
            self.run_ocr("tesseract", major=16, installed=True)
        self.assertIn("PaddleOCR only", logs.output[0])

    def test_plugin_installed_reads_the_entry_points(self) -> None:
        class EP:
            def __init__(self, value: str) -> None:
                self.value = value

        with mock.patch("importlib.metadata.entry_points", return_value=[EP(ocr.PLUGIN)]) as eps:
            self.assertTrue(ocr.plugin_installed())
        eps.assert_called_once_with(group="ocrmypdf")
        with mock.patch("importlib.metadata.entry_points", return_value=[EP("other.plugin")]):
            self.assertFalse(ocr.plugin_installed())

    def test_paddle_failure_falls_back_to_tesseract(self) -> None:
        with self.assertLogs("tidyscan.ocr", "WARNING") as logs:
            self.run_ocr(fail_paddle=True)
        self.assertEqual(["--plugin" in c for c in self.calls], [True, False])
        self.assertIn("paddle-broke", logs.output[0])

    def test_both_fail(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            fake_bin("ocrmypdf", "echo broken >&2; exit 3\n"),
            mock.patch("tidyscan.paddle.missing", return_value=[]),
            self.assertLogs("tidyscan.ocr", "WARNING"),
        ):
            with self.assertRaisesRegex(ocr.OcrError, "exit 3: broken"):
                ocr.add_text_layer(b"%PDF-plain", "eng", tmp, engine="auto")
            self.assertEqual(os.listdir(tmp), [])

    def test_unknown_engine(self) -> None:
        with self.assertRaises(ValueError):
            ocr.add_text_layer(b"%PDF-x", "eng", engine="easyocr")


def text_page(lines: Sequence[str], size: tuple[int, int] = (1700, 1000)) -> tuple[bytes, Image.Image]:
    """A 1-bit PBM page with the given lines of text, and its PIL image."""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    for n, text in enumerate(lines):
        d.text((120, 150 + 110 * n), text, fill=0, font=ImageFont.load_default(size=56))
    im = im.convert("1")
    pbm = b"P4\n%d %d\n" % ((im.width + 7) // 8 * 8, im.height) + bytes(x ^ 0xFF for x in im.tobytes())
    return pbm, im


PANGRAM = ["The quick brown fox jumps", "over the lazy dog 1234"]


def stub_read(path: str | os.PathLike[str]) -> tuple[str, str]:
    """PaddleOCR's answer for any page: an item and its price, one row with two boxes."""
    from PIL import Image

    with Image.open(path) as im:
        w, h = im.size
    return hocr.page(
        w,
        h,
        [
            hocr.Line("Milk", (w // 10, h // 10, w // 4, h // 10 + 40), 0.9),
            hocr.Line("1.29", (w // 2, h // 10 + 2, w // 2 + 150, h // 10 + 42), 0.8),
        ],
    )


@unittest.skipUnless(importlib.util.find_spec("ocrmypdf") and shutil.which("tesseract"), "needs ocrmypdf and tesseract")
class OcrmypdfPluginTest(unittest.TestCase):
    """The plugin in a real OCRmyPDF run, 16 or 17, with PaddleOCR replaced by stub_read. Tesseract
    must be installed (OCRmyPDF checks it) but is not what reads the page."""

    # Any: OCRmyPDF options, passed through to ocrmypdf.ocr unchanged
    def run_ocrmypdf(self, **kwargs: Any) -> tuple[bytes, str]:  # noqa: ANN401
        import ocrmypdf

        pbm, _ = text_page(PANGRAM)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(paddle, "read", stub_read),
            mock.patch.object(paddle, "missing", return_value=[]),
            mock.patch.object(paddle, "versions", return_value="stub"),
        ):
            src, dst, side = (os.path.join(tmp, n) for n in ("in.pdf", "out.pdf", "out.txt"))
            with open(src, "wb") as f:
                f.write(pdf.build([pbm], 300))
            ocrmypdf.ocr(
                src,
                dst,
                plugins=["tidyscan.ocrmypdf_paddle"],
                language="eng",
                output_type="pdf",
                sidecar=side,
                use_threads=True,
                jobs=1,
                progress_bar=False,
                **kwargs,
            )
            with open(dst, "rb") as f, open(side, encoding="utf-8") as g:
                return f.read(), g.read()

    def test_text_layer_from_the_plugin(self) -> None:
        out, text = self.run_ocrmypdf()
        self.assertIn(b"PaddleOCR PP-OCRv5 (stub)", out)  # the creator tag
        self.assertEqual(text.split(), ["Milk", "1.29"])
        if shutil.which("gs"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "o.pdf")
                with open(path, "wb") as f:
                    f.write(out)
                layer = subprocess.run(
                    ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=txtwrite", "-o", "-", path],
                    capture_output=True,
                    check=True,
                ).stdout.decode()
            self.assertEqual([ln.split() for ln in layer.splitlines() if ln.strip()], [["Milk", "1.29"]])

    def test_sandwich_is_refused(self) -> None:
        from ocrmypdf.exceptions import BadArgsError

        with self.assertRaises(BadArgsError):
            self.run_ocrmypdf(pdf_renderer="sandwich")

    def test_other_engine_chosen_on_17(self) -> None:
        from tidyscan import ocrmypdf_paddle as plugin

        if not plugin.SELECTABLE:
            self.skipTest("OCRmyPDF 16 has no --ocr-engine")
        from types import SimpleNamespace
        from typing import cast

        # the OCRmyPDF 17 form, get_ocr_engine(options); a type checker sees the 16 form defined last
        get_ocr_engine = cast("Callable[..., object]", plugin.get_ocr_engine)
        self.assertIsNone(get_ocr_engine(SimpleNamespace(ocr_engine="tesseract")))
        self.assertIsNotNone(get_ocr_engine(SimpleNamespace(ocr_engine="paddle")))
        self.assertIsNotNone(get_ocr_engine(None))
        out, _ = self.run_ocrmypdf(ocr_engine="paddle")
        self.assertIn(b"PaddleOCR PP-OCRv5 (stub)", out)


@unittest.skipUnless(
    not paddle.missing() and importlib.util.find_spec("ocrmypdf"), "needs the PaddleOCR runtime and models"
)
class PaddleIntegrationTest(unittest.TestCase):
    def test_reads_offline(self) -> None:
        import socket

        def no_network(*a: object, **k: object) -> Never:
            raise AssertionError("network access attempted")

        _, im = text_page(PANGRAM)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(socket.socket, "connect", no_network),
            mock.patch("socket.create_connection", no_network),
            mock.patch("socket.getaddrinfo", no_network),
        ):
            path = os.path.join(tmp, "p.png")
            im.save(path, dpi=(300, 300))
            paddle._engine = None  # load the models with the network blocked
            doc, text = paddle.read(path)
        words = text.split()
        for w in ("quick", "brown", "fox", "lazy", "1234"):
            self.assertIn(w, words)
        self.assertIn('class="ocrx_word"', doc)

    def test_text_layer_through_ocrmypdf(self) -> None:
        pbm, _ = text_page(PANGRAM)
        out = ocr.add_text_layer(pdf.build([pbm], 300), "nld+eng", engine="paddle")
        self.assertIn(b"PaddleOCR", out)  # the creator tag: Paddle did read
        if shutil.which("gs"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "o.pdf")
                with open(path, "wb") as f:
                    f.write(out)
                text = subprocess.run(
                    ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=txtwrite", "-o", "-", path],
                    capture_output=True,
                    check=True,
                ).stdout.decode()
            for w in ("quick", "fox", "lazy"):
                self.assertIn(w, text.split())


class CliEngineTest(unittest.TestCase):  # was a second CliTest, which hid this one
    def test_engine_flag(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("tidyscan.ocr.add_text_layer", return_value=b"%PDF-o") as a,
        ):
            path = os.path.join(tmp, "p.pbm")
            with open(path, "wb") as f:
                f.write(fake_pbm())
            out = os.path.join(tmp, "o.pdf")
            self.assertEqual(
                cli.main(
                    ["-q", "--no-rotate", "--no-deskew", "--ocr", "nld", "--engine", "tesseract", "-o", out, path]
                ),
                0,
            )
            self.assertEqual(a.call_args.kwargs["engine"], "tesseract")
            cli.main(["-q", "--no-rotate", "--no-deskew", "--ocr", "nld", "-o", out, path])
            self.assertEqual(a.call_args.kwargs["engine"], "tesseract")  # the default
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["--engine", "easyocr", "-o", out, path])


def document_folder(root: str, function: str, name: str, pages: list[bytes], **extra: object) -> str:
    """A document folder as scankeyd writes it: root/<function>/<name>/ with the pages and document.json."""
    folder = os.path.join(root, function, name)
    os.makedirs(folder)
    names = []
    for n, page in enumerate(pages, 1):
        names.append(f"page-{n:04d}.{'pbm' if page.startswith(b'P4') else 'jpg'}")
        with open(os.path.join(folder, names[-1]), "wb") as f:
            f.write(page)
    doc = {"format": 1, "function": function.upper(), "resolution": 300, "pages": names, **extra}
    with open(os.path.join(folder, "document.json"), "w") as f:
        json.dump(doc, f)
    return folder


class WatchTest(unittest.TestCase):
    def test_documents_to_pdfs_oldest_first(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "file", "scan_b", [fake_pbm(), fake_jpeg()], scanned="2026-11-06T10:00:02")
            document_folder(src, "image", "scan_a", [fake_jpeg()], scanned="2026-11-06T10:00:01")
            os.makedirs(os.path.join(src, "ocr", ".scan_c.part"))  # still being written: not touched
            self.assertEqual([os.path.basename(d) for d in watch.documents(src)], ["scan_a", "scan_b"])
            self.assertEqual(watch.run_once(src, out, watch.Options(rotate=False, deskew=False)), 2)
            self.assertEqual(sorted(os.listdir(out)), ["scan_a.pdf", "scan_b.pdf"])
            with open(os.path.join(out, "scan_b.pdf"), "rb") as f:
                self.assertIn(b"/Count 2", f.read())
            self.assertEqual(sorted(os.listdir(os.path.join(src, "file"))), [])  # done: gone
            self.assertTrue(os.path.isdir(os.path.join(src, "ocr", ".scan_c.part")))

    def test_text_layer_only_for_the_ocr_folders_and_keep_folders(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp, fake_bin("ocrmypdf", OcrTest.COPY):
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "ocr", "letter", [fake_pbm()])
            document_folder(src, "image", "photo", [fake_jpeg()])
            opts = watch.Options(ocr_languages="nld+eng", keep_folders=True, rotate=False, deskew=False)
            self.assertEqual(watch.run_once(src, out, opts), 2)
            with open(os.path.join(out, "ocr", "letter.pdf"), "rb") as f:
                self.assertTrue(f.read().endswith(b"%OCR-nld+eng"))
            with open(os.path.join(out, "image", "photo.pdf"), "rb") as f:
                self.assertFalse(f.read().endswith(b"%OCR-nld+eng"))

    def test_unreadable_document_moves_aside_and_names_do_not_clash(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            with open(os.path.join(out, "scan.pdf"), "wb") as f:
                f.write(b"%PDF-earlier")
            document_folder(src, "file", "scan", [fake_jpeg()])
            document_folder(src, "file", "broken", [b"not an image"])
            with self.assertLogs("tidyscan.watch", "ERROR"):
                self.assertEqual(watch.run_once(src, out, watch.Options(rotate=False, deskew=False)), 1)
            self.assertEqual(sorted(os.listdir(out)), ["scan.pdf", "scan_2.pdf"])  # the earlier one kept
            self.assertEqual(os.listdir(os.path.join(src, ".failed")), ["broken"])

    def test_cli_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "email", "mail", [fake_pbm()])
            self.assertEqual(cli.main(["watch", "-q", "--once", "--no-rotate", "--no-deskew", src, out]), 0)
            self.assertEqual(os.listdir(out), ["mail.pdf"])
            self.assertEqual(cli.main(["watch", "-q", "--once", os.path.join(tmp, "nope"), out]), 2)

    def test_status_file(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            status_path = os.path.join(tmp, "status.json")
            status = watch.Status(status_path, src)
            document_folder(src, "file", "good", [fake_jpeg()], scanned="2026-11-06T10:00:01")
            document_folder(src, "file", "zbroken", [b"not an image"], scanned="2026-11-06T10:00:02")
            with self.assertLogs("tidyscan.watch", "ERROR"):
                watch.run_once(src, out, watch.Options(rotate=False, deskew=False), status=status)
            with open(status_path) as f:
                st = json.load(f)
            self.assertEqual((st["last_file"], st["failed"], st["waiting"]), ("good.pdf", 1, 0))
            self.assertIn("zbroken: moved to .failed", st["last_error"])
            old = document_folder(src, "file", "stuck", [fake_jpeg()])  # an old one nobody converts
            os.utime(os.path.join(old, "document.json"), (0, 0))
            status.write(force=True)
            with open(status_path) as f:
                self.assertEqual(json.load(f)["waiting"], 1)
            document_folder(src, "file", "next", [fake_jpeg()])
            shutil.rmtree(old)
            watch.run_once(src, out, watch.Options(rotate=False, deskew=False), status=status)
            with open(status_path) as f:
                st = json.load(f)
            self.assertEqual((st["last_file"], st["last_error"], st["waiting"]), ("next.pdf", "", 0))
            self.assertEqual((st["pdfs"], st["unreadable"], st["no_text"]), (2, 1, 0))
            again = watch.Status(status_path, src)  # a restart: the counts go on
            again.ok("third.pdf", "third: written without text, OCR failed (x)")
            with open(status_path) as f:
                st = json.load(f)
            self.assertEqual((st["pdfs"], st["unreadable"], st["no_text"]), (3, 1, 1))

    def test_the_document_ocr_wish_decides(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp, fake_bin("ocrmypdf", OcrTest.COPY):
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "file", "wanted", [fake_pbm()], ocr=True)  # FUNC_FILE=bw,ocr
            document_folder(src, "ocr", "declined", [fake_pbm()], ocr=False)  # FUNC_OCR=bw
            document_folder(src, "ocr", "older", [fake_pbm()])  # no wish: the folder list (ocr)
            watch.run_once(src, out, watch.Options(rotate=False, deskew=False))
            for name, text in (("wanted", True), ("declined", False), ("older", True)):
                with open(os.path.join(out, name + ".pdf"), "rb") as f:
                    self.assertEqual(f.read().endswith(b"%OCR-eng"), text, name)

    def test_settings_from_the_shared_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, fake_bin("ocrmypdf", OcrTest.COPY):
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "ocr", "letter", [fake_pbm()], ocr=True)
            env = {"OCR_LANGUAGES": "nld+eng", "ROTATE": "off", "DESKEW": "off"}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch("tidyscan.watch.straighten", return_value=(0, 0.0)) as s,
            ):
                self.assertEqual(cli.main(["watch", "-q", "--once", src, out]), 0)
            self.assertEqual((s.call_args.kwargs["rotate"], s.call_args.kwargs["deskew"]), (False, False))
            with open(os.path.join(out, "letter.pdf"), "rb") as f:
                self.assertTrue(f.read().endswith(b"%OCR-nld+eng"))
            with mock.patch.dict(os.environ, {"ROTATE": "maybe"}), self.assertRaises(SystemExit):
                cli.main(["watch", "-q", "--once", src, out])


class ReviewFixesTest(unittest.TestCase):
    """What the review before the first release found (06-11-2026)."""

    def test_a_malformed_document_moves_aside_and_the_rest_goes_on(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            bad = {
                "list": '{"resolution": [300], "pages": ["page-0001.pbm"]}',
                "huge": '{"resolution": 1e400, "pages": ["page-0001.pbm"]}',
                "pages": '{"resolution": 300, "pages": 5}',
                "deep": '{"resolution": ' + "[" * 100000 + "]" * 100000 + "}",
            }
            for name, text in bad.items():
                folder = document_folder(src, "file", name, [fake_pbm()])
                with open(os.path.join(folder, "document.json"), "w") as f:
                    f.write(text)
            document_folder(src, "file", "good", [fake_pbm()])
            with self.assertLogs("tidyscan.watch", "ERROR"):
                self.assertEqual(watch.run_once(src, out, watch.Options(rotate=False, deskew=False)), 1)
            self.assertEqual(os.listdir(out), ["good.pdf"])
            self.assertEqual(sorted(os.listdir(os.path.join(src, watch.FAILED))), sorted(bad))

    def test_a_folder_that_cannot_be_removed_is_not_converted_twice(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "file", "scan", [fake_pbm()])
            if os.geteuid() == 0:
                self.skipTest("root removes it anyway")
            stuck: set[str] = set()
            opts = watch.Options(rotate=False, deskew=False)
            os.chmod(os.path.join(src, "file"), 0o555)  # the folder's own parent: it cannot go
            try:
                with self.assertLogs("tidyscan.watch", "ERROR"):
                    self.assertEqual(watch.run_once(src, out, opts, stuck=stuck), 1)
                self.assertEqual(watch.run_once(src, out, opts, stuck=stuck), 0)
            finally:
                os.chmod(os.path.join(src, "file"), 0o755)
            self.assertEqual(os.listdir(out), ["scan.pdf"])

    def test_a_function_is_never_a_path(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            folder = document_folder(src, "file", "scan", [fake_pbm()])
            with open(os.path.join(folder, "document.json"), "w") as f:
                json.dump({"function": "../../escaped", "resolution": 300, "pages": ["page-0001.pbm"]}, f)
            opts = watch.Options(rotate=False, deskew=False, keep_folders=True)
            self.assertEqual(watch.run_once(src, out, opts), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "escaped")))
            self.assertEqual(os.listdir(os.path.join(src, watch.FAILED)), ["scan"])

    def test_watch_refuses_a_bad_engine_and_an_input_it_cannot_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(src)
            os.makedirs(out)
            with (
                mock.patch.dict(os.environ, {"OCR_ENGINE": "papyrus"}),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cli.main(["watch", "-q", "--once", src, out])
            if os.geteuid() != 0:  # root may write anyway
                os.chmod(src, 0o555)
                try:
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(cli.main(["watch", "-q", "--once", src, out]), 2)
                    self.assertIn("not writable", err.getvalue())
                finally:
                    os.chmod(src, 0o755)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "needs Pillow")
    def test_16_bit_gray_is_scaled_not_clipped(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.png")
            img = Image.new("I;16", (16, 16), 30000)
            img.save(path)
            (page,) = cli._load(path)
            self.assertTrue(page.startswith(b"\xff\xd8"))
            pixel = Image.open(io.BytesIO(page)).getpixel((8, 8))
            assert isinstance(pixel, int)
            self.assertAlmostEqual(pixel, 117, delta=2)  # 30000 / 256; clipped it was 255

    def test_pbm_with_a_comment_and_a_trailing_newline(self) -> None:
        rows = bytes(range(8)) * 2  # 16 x 8 pixels: 2 bytes a row
        w, h, got = pdf.pbm_info(b"P4\n# Created by GIMP\n16 8\n" + rows + b"\n")
        self.assertEqual((w, h, got), (16, 8, rows))
        with self.assertRaises(pdf.UnsupportedPage):
            pdf.pbm_info(b"P4\n16 8\n" + rows[:-1])

    def test_adobe_cmyk_is_inverted(self) -> None:
        plain = fake_jpeg(comps=4)
        adobe = plain[:2] + b"\xff\xee\x00\x0eAdobe\x00\x64\x00\x00\x00\x00\x02" + plain[2:]
        self.assertNotIn(b"/Decode", pdf.build([plain], 300))
        self.assertIn(b"/DeviceCMYK /Decode [1 0 1 0 1 0 1 0]", pdf.build([adobe], 300))

    def test_page_budget_and_too_large_pages(self) -> None:
        from tidyscan import _budget

        token = _budget.start(0)
        try:
            with self.assertLogs("tidyscan._budget", "WARNING"), self.assertRaises(subprocess.TimeoutExpired):
                _budget.timeout(60)
        finally:
            _budget.end(token)
        self.assertEqual(_budget.timeout(60), 60)  # no budget outside straighten()
        with mock.patch("tidyscan.skew.detect") as detect, self.assertLogs("tidyscan", "WARNING"):
            self.assertEqual(tidyscan.straighten(fake_jpeg(width=20000, height=20000), 300), (0, 0.0))
        detect.assert_not_called()


class SecondReviewTest(unittest.TestCase):
    """What the second review before the first release found (06-11-2026)."""

    def test_build_takes_every_kind_of_page(self) -> None:
        pbm = fake_pbm()

        class Rows:
            def to_pbm(self) -> bytes:
                return pbm

        self.assertEqual(pdf.build([memoryview(pbm)], 300), pdf.build([pbm], 300))
        self.assertEqual(pdf.build([Rows()], 300), pdf.build([pbm], 300))

    def test_jpeg_fill_bytes(self) -> None:
        jpeg = fake_jpeg(width=40, height=30)
        self.assertEqual(pdf.jpeg_info(jpeg[:2] + b"\xff\xff" + jpeg[2:])[:2], (40, 30))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "needs Pillow")
    def test_every_frame_of_a_tiff_and_dpi_checked(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path, out = os.path.join(tmp, "m.tif"), os.path.join(tmp, "m.pdf")
            frames = [Image.new("L", (20, 20), v) for v in (0, 128, 255)]
            frames[0].save(path, "TIFF", save_all=True, append_images=frames[1:])
            self.assertEqual(len(cli._load(path)), 3)
            self.assertEqual(cli.main(["-q", "--no-rotate", "--no-deskew", "-o", out, path]), 0)
            with open(out, "rb") as f:
                self.assertIn(b"/Count 3", f.read())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["-q", "--dpi", "0", "-o", out, path])

    def test_stuck_folders_are_remembered_over_a_restart(self) -> None:
        from tidyscan import watch

        if os.geteuid() == 0:
            self.skipTest("root removes it anyway")
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            document_folder(src, "file", "scan", [fake_pbm()])
            opts = watch.Options(rotate=False, deskew=False)
            os.chmod(os.path.join(src, "file"), 0o555)
            try:
                with self.assertLogs("tidyscan.watch", "ERROR"):
                    self.assertEqual(watch.run_once(src, out, opts), 1)
                self.assertEqual(watch.run_once(src, out, opts), 0)  # a new process: read from IN
                self.assertEqual(watch.unwritable(src), [os.path.join(src, "file")])
            finally:
                os.chmod(os.path.join(src, "file"), 0o755)
            self.assertEqual(os.listdir(out), ["scan.pdf"])

    def test_on_off_settings_and_all_folders(self) -> None:
        from tidyscan import watch

        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out")
            os.makedirs(out)
            folder = document_folder(src, "receipts", "bon", [fake_pbm()])
            with open(os.path.join(folder, "document.json"), "w") as f:
                json.dump({"resolution": 300, "pages": ["page-0001.pbm"]}, f)  # no ocr wish
            with (
                fake_bin("ocrmypdf", OcrTest.COPY),
                mock.patch.dict(os.environ, {"OCR_FOLDERS": "all", "PDFA": "no", "ROTATE": "false", "DESKEW": "0"}),
                mock.patch("tidyscan.watch.straighten", return_value=(0, 0.0)),
            ):
                self.assertEqual(cli.main(["watch", "-q", "--once", src, out]), 0)
            with open(os.path.join(out, "bon.pdf"), "rb") as f:
                self.assertTrue(f.read().endswith(b"%OCR-eng"))
            with mock.patch.dict(os.environ, {"PDFA": "auto"}), self.assertRaises(SystemExit):
                cli.main(["watch", "-q", "--once", src, out])
        self.assertEqual(watch.Options().ocr_folders, ("ocr",))


class CliTest(unittest.TestCase):
    def test_pages_to_pdf(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("tidyscan.skew.detect", return_value=2.0),
            mock.patch("tidyscan.orientation.detect", return_value=(180, True)),
            fake_bin("ocrmypdf", OcrTest.COPY),
        ):
            paths = []
            for n, data in enumerate((fake_jpeg(), fake_pbm())):
                paths.append(os.path.join(tmp, f"p{n}"))
                with open(paths[-1], "wb") as f:
                    f.write(data)
            out = os.path.join(tmp, "out.pdf")
            self.assertEqual(cli.main(["-q", "--ocr", "eng", "-o", out] + paths), 0)
            with open(out, "rb") as f:
                data = f.read()
            self.assertEqual(re.findall(rb"/Rotate (\d+)", data), [b"180", b"180"])
            self.assertTrue(data.endswith(b"%OCR-eng"))

    def test_bad_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.pbm")
            with open(path, "wb") as f:
                f.write(b"P4\n8 8\n\x00")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(
                    cli.main(["-q", "--no-rotate", "--no-deskew", "-o", os.path.join(tmp, "o.pdf"), path]), 1
                )
            self.assertIn("PBM size", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class FakeDownload(io.BytesIO):
    """What urlopen returns, for models.fetch(); counts the URLs asked for."""


class ModelsTest(unittest.TestCase):
    """models.fetch() and the lookup order, with fake assets and no network."""

    DATA: ClassVar[dict[str, bytes]] = {"a.onnx": b"detector", "b.onnx": b"recogniser", "b.txt": b"a\nb\n"}

    def setUp(self) -> None:
        import hashlib

        self.assets = {
            "a.onnx": models.Asset("det/model.onnx", hashlib.sha256(self.DATA["a.onnx"]).hexdigest(), 100),
            "b.onnx": models.Asset("rec/model.onnx", hashlib.sha256(self.DATA["b.onnx"]).hexdigest(), 100),
            "b.txt": models.Asset("rec/keys.txt", hashlib.sha256(self.DATA["b.txt"]).hexdigest(), 100),
        }
        for patcher in (
            mock.patch.dict(models.ASSETS, self.assets, clear=True),
            mock.patch.object(models, "MODEL_FILES", tuple(self.assets)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.urls: list[str] = []

    def opener(self, data: dict[str, bytes]) -> Callable[[str, float], FakeDownload]:
        def open_url(url: str, timeout: float) -> FakeDownload:
            self.assertGreater(timeout, 0)
            self.urls.append(url)
            return FakeDownload(data[url.rsplit("/", 1)[1]])

        return open_url

    def test_fetch_writes_checked_files_in_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = models.fetch(tmp, opener=self.opener(self.DATA))
            self.assertEqual(
                sorted(os.path.relpath(p, tmp) for p in written), sorted(a.path for a in self.assets.values())
            )
            with open(os.path.join(tmp, "rec", "keys.txt"), "rb") as f:
                self.assertEqual(f.read(), self.DATA["b.txt"])
            self.assertTrue(models.complete(tmp))
            self.assertTrue(all(f"/download/{models.TAG}/" in u for u in self.urls))

    def test_existing_correct_files_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models.fetch(tmp, opener=self.opener(self.DATA))
            self.urls.clear()
            self.assertEqual(models.fetch(tmp, opener=self.opener(self.DATA)), [])
            self.assertEqual(self.urls, [])

    def test_a_wrong_download_is_refused_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = {**self.DATA, "b.onnx": b"something else"}
            with self.assertRaisesRegex(models.ModelError, "sha256"):
                models.fetch(tmp, opener=self.opener(bad))
            self.assertFalse(os.path.exists(os.path.join(tmp, "rec", "model.onnx")))
            self.assertEqual([n for n in os.listdir(os.path.join(tmp, "rec")) if n.endswith(".part")], [])

    def test_a_wrong_existing_file_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "det"))
            with open(os.path.join(tmp, "det", "model.onnx"), "wb") as f:
                f.write(b"old")
            models.fetch(tmp, opener=self.opener(self.DATA))
            with open(os.path.join(tmp, "det", "model.onnx"), "rb") as f:
                self.assertEqual(f.read(), self.DATA["a.onnx"])

    def test_too_large_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = {**self.DATA, "a.onnx": b"x" * 101}
            with self.assertRaisesRegex(models.ModelError, "larger"):
                models.fetch(tmp, opener=self.opener(big))
            self.assertFalse(os.path.exists(os.path.join(tmp, "det", "model.onnx")))

    def test_a_broken_connection_is_a_model_error(self) -> None:
        def broken(url: str, timeout: float) -> Never:
            raise OSError("timed out")

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(models.ModelError, "timed out"):
            models.fetch(tmp, opener=broken)

    def test_lookup_order(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as env_dir:
            data_home = os.path.join(home, "data")
            user = os.path.join(data_home, "tidyscan", "models")
            env = {"XDG_DATA_HOME": data_home}
            with mock.patch.dict(os.environ, env), mock.patch.object(models, "SYSTEM_DIR", os.path.join(home, "none")):
                os.environ.pop(models.MODELS_ENV, None)
                self.assertEqual(models.model_dir(), user)  # nothing anywhere: where fetch() puts them
                self.assertEqual(models.fetch(opener=self.opener(self.DATA))[0].startswith(user), True)
                self.assertEqual(models.model_dir(), user)
                os.environ[models.MODELS_ENV] = env_dir  # the variable wins, even when empty
                self.assertEqual(models.model_dir(), env_dir)
                self.assertIn("incomplete", models.describe())
            system = os.path.join(home, "system")
            os.makedirs(system)
            models.fetch(system, opener=self.opener(self.DATA))
            with (
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(home, "empty")}),
                mock.patch.object(models, "SYSTEM_DIR", system),
            ):
                os.environ.pop(models.MODELS_ENV, None)
                self.assertEqual(models.model_dir(), system)  # no per-user copy: the system one

    def test_paddle_reads_the_models_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {models.MODELS_ENV: tmp}):
            self.assertEqual(paddle.model_dir(), tmp)

    def test_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(models, "_urlopen", self.opener(self.DATA)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["models", "fetch", tmp]), 0)
                self.assertEqual(cli.main(["models", "fetch", tmp]), 0)
            self.assertIn("3 file(s) fetched", out.getvalue())
            self.assertIn("models present", out.getvalue())
            with mock.patch.dict(os.environ, {models.MODELS_ENV: tmp}), contextlib.redirect_stdout(io.StringIO()) as o:
                self.assertEqual(cli.main(["models", "path"]), 0)
            self.assertIn(f"used: {tmp}", o.getvalue())


class ModelAssetsTest(unittest.TestCase):
    TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "models.py")

    def test_the_notice_and_licence_match_their_pinned_hashes(self) -> None:
        import hashlib
        import runpy

        tools = runpy.run_path(self.TOOLS, run_name="tidyscan_tools_models")
        notice = tools["notice"]().encode()
        self.assertEqual(hashlib.sha256(notice).hexdigest(), models.ASSETS["MODELS-NOTICE.txt"].sha256)
        self.assertEqual(models.sha256(tools["APACHE"]), models.ASSETS["LICENSE-Apache-2.0.txt"].sha256)
        for name in models.MODEL_FILES:  # the notice lists every model file with its hash
            self.assertIn(f"{name}  {models.ASSETS[name].sha256}".encode(), notice)

    def test_real_assets_match_the_paddle_layout(self) -> None:
        self.assertEqual({models.ASSETS[n].path for n in models.MODEL_FILES}, {paddle.DET, paddle.REC, paddle.KEYS})
        self.assertLessEqual({"MODELS-NOTICE.txt", "LICENSE-Apache-2.0.txt"}, set(models.ASSETS))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", a.sha256) for a in models.ASSETS.values()))
