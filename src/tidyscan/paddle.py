"""PaddleOCR PP-OCRv5 (mobile text detector, Latin recogniser) through RapidOCR on OpenVINO, on the CPU.

The models are local ONNX files, never downloaded implicitly: `tidyscan models fetch` puts them
in place once, and models.py says where they are looked up ($TIDYSCAN_PADDLE_MODELS, the per-user
directory, /opt/tidyscan/models), laid out as

    PP-OCRv5_mobile_det/model.onnx
    latin_PP-OCRv5_mobile_rec/model.onnx
    latin_PP-OCRv5_mobile_rec/keys.txt      the recogniser's characters, one per line

TIDYSCAN_PADDLE_THREADS sets OpenVINO's inference threads; by default they follow the container's
CPU quota (cgroup cpu.max), else OpenVINO takes every CPU it sees.
The Latin recogniser reads Dutch, English and the other languages in LANGUAGES; there is no language
choice to make, so the OCR languages only steer Tesseract.

Needs `pip install 'tidyscan[paddle]'` (rapidocr, openvino, opencv-python-headless).
"""

from __future__ import annotations

import importlib.util
import os
from typing import TYPE_CHECKING, Protocol

from . import hocr, models

if TYPE_CHECKING:
    from collections.abc import Sequence

MODELS_ENV = models.MODELS_ENV
THREADS_ENV = "TIDYSCAN_PADDLE_THREADS"
DET, REC, KEYS = models.DET, models.REC, models.KEYS
MODULES = ("rapidocr", "openvino", "cv2", "numpy", "PIL")

# Tesseract codes of the languages PaddleOCR's latin_PP-OCRv5 recogniser is trained for.
LANGUAGES = frozenset(
    """
    afr bos ces cym dan deu eng est fra gle hrv hun ind isl ita lat lit mri msa nld nor oci pol por
    slk slv spa sqi srp_latn swa swe tgl tur uzb
""".split()
)

_engine = None


def model_dir() -> str:
    return models.model_dir()


def missing() -> list[str]:
    """What this engine lacks here: Python modules and model files; empty when it can run."""
    out = [m for m in MODULES if importlib.util.find_spec(m) is None]
    out += [os.path.join(model_dir(), f) for f in (DET, REC, KEYS) if not os.path.isfile(os.path.join(model_dir(), f))]
    return out


def covers(languages: str) -> bool:
    """True when every language of 'eng+nld' is one the Latin recogniser reads."""
    return all(lang in LANGUAGES for lang in languages.split("+"))


def versions() -> str:
    from importlib.metadata import version

    return f"RapidOCR {version('rapidocr')}, OpenVINO {version('openvino').split('-')[0]}"


class RapidOcrResult(Protocol):
    """The parts of a RapidOCR result this module reads (RapidOCR 3.x, `return_word_box=True`).

    All None for a page without text."""

    @property
    def boxes(self) -> Sequence[Sequence[Sequence[float]]] | None: ...  # a quadrilateral per line
    @property
    def txts(self) -> Sequence[str] | None: ...
    @property
    def scores(self) -> Sequence[float] | None: ...
    @property  # per line: (text, score, quadrilateral) per word
    def word_results(self) -> Sequence[Sequence[tuple[str, float, Sequence[Sequence[float]] | None]]] | None: ...


class RapidOcrReader(Protocol):
    """A RapidOCR instance, as called by read()."""

    def __call__(self, image_path: str, *, return_word_box: bool) -> RapidOcrResult | None: ...


def params() -> dict[str, object]:
    """RapidOCR settings: PaddleOCR's own for these models (their inference.yml and PaddleOCR 3's
    PP-OCRv5 pipeline) except the detector's resolution, and RapidOCR's default text score (0.5)."""
    from rapidocr import EngineType

    d = model_dir()
    p = {
        "Global.log_level": "warning",
        "Global.use_cls": False,  # pages arrive upright: tidyscan turned them
        # RapidOCR's default of 2000 would shrink a 300-dpi A4 page (2480x3508) and long receipts
        "Global.max_side_len": 4400,
        "Det.engine_type": EngineType.OPENVINO,
        "Det.model_path": os.path.join(d, DET),
        # The detector sees the page at most 2000 pixels long; the recogniser still reads the text
        # from the full-size page. Measured 01-11-2026 on two receipts of 102 words each, rasterised
        # by OCRmyPDF at 3856x4212 and 3706x4164 (2 CPUs): at full size (PaddleOCR's limit_type min)
        # recall 82% / precision 77% averaged, 3.1 GiB peak, 19 s; at 2000 84% / 80%, 0.96 GiB, 9 s.
        # 960 to 2400 all scored within a few words of each other.
        "Det.limit_side_len": 2000,
        "Det.limit_type": "max",
        "Det.mean": [0.485, 0.456, 0.406],
        "Det.std": [0.229, 0.224, 0.225],
        "Det.thresh": 0.3,
        "Det.box_thresh": 0.6,
        "Det.unclip_ratio": 1.5,
        "Det.use_dilation": False,
        "Rec.engine_type": EngineType.OPENVINO,
        "Rec.model_path": os.path.join(d, REC),
        "Rec.rec_keys_path": os.path.join(d, KEYS),
        "Rec.rec_img_shape": [3, 48, 320],
    }
    threads = threads_wanted()
    if threads:
        p["EngineConfig.openvino.inference_num_threads"] = threads
    return p


def threads_wanted() -> int | None:
    """OpenVINO's thread count: TIDYSCAN_PADDLE_THREADS, else the CPU quota, else None (all).

    OpenVINO counts the CPUs it may run on, not a quota: with 4 threads under a 2-CPU quota a page
    took 8.8 s, with 2 threads 4.6 s (01-11-2026, N150)."""
    raw = os.environ.get(THREADS_ENV, "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            return max(1, min(len(os.sched_getaffinity(0)), -(-int(quota) // int(period))))
    except (OSError, ValueError, AttributeError):
        pass
    return None


def engine() -> RapidOcrReader:
    """The RapidOCR instance, made once per process (loading the models takes a second or two)."""
    global _engine
    if _engine is None:
        lacking = missing()
        if lacking:
            raise RuntimeError("PaddleOCR engine not available, missing: " + ", ".join(lacking))
        from rapidocr import RapidOCR

        _engine = RapidOCR(params=params())
    return _engine


def lines(result: RapidOcrResult | None, width: int, height: int) -> list[hocr.Line]:
    """hocr.Line objects from a RapidOCR result (boxes, txts, scores, word_results).

    RapidOCR's word boxes are used when they belong to their line (same characters); otherwise the
    line's box is divided over its words by character position."""
    txts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    if result is None or txts is None or boxes is None:
        return []
    scores = result.scores
    if scores is None:
        raise TypeError("RapidOCR result with texts but without scores")
    words = getattr(result, "word_results", None) or ()
    aligned = len(words) == len(txts)
    out = []
    # strict=False: RapidOCR's lists normally match; a short one ends the page's lines as before
    for i, (quad, text, score) in enumerate(zip(boxes, txts, scores, strict=False)):
        box = hocr.bbox(quad, width, height)
        ws = []
        if aligned and words[i] and "".join(str(w[0]) for w in words[i]).replace(" ", "") == text.replace(" ", ""):
            ws = [
                hocr.Word(str(w[0]), hocr.bbox(w[2], width, height), float(w[1]))
                for w in words[i]
                if w[2] is not None and str(w[0]).strip()
            ]
        out.append(hocr.Line(text, box, float(score), ws))
    return out


def read(image_path: str | os.PathLike[str]) -> tuple[str, str]:
    """(hOCR, plain text) of a page image file."""
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    result = engine()(str(image_path), return_word_box=True)
    return hocr.page(width, height, lines(result, width, height), system="tidyscan PaddleOCR PP-OCRv5")
