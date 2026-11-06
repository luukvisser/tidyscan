"""PaddleOCR's PP-OCRv5 models for the paddle engine: where they are looked up, and fetching them.

The models are ONNX files converted from PaddlePaddle's own PP-OCRv5 models (Apache-2.0) by
tools/models.py. tidyscan never downloads them by itself: `tidyscan models fetch` (or
fetch()) takes them, once, from this project's release `TAG` and checks each against the sha256
below, which tools/models.py also checks its own conversion against. This module is the single
source of those names and hashes; it uses the standard library only and imports nothing else from
the package, so tools/models.py can load it from its path.

Lookup order (model_dir()): $TIDYSCAN_PADDLE_MODELS when set; else the first of the per-user
directory ($XDG_DATA_HOME/tidyscan/models, default ~/.local/share/tidyscan/models) and
/opt/tidyscan/models that holds all three files; else the per-user directory, where fetch()
puts them.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import secrets
import urllib.request
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

MODELS_ENV = "TIDYSCAN_PADDLE_MODELS"
SYSTEM_DIR = "/opt/tidyscan/models"

# The release that holds the converted models, separate from the package's own versions: the
# models change rarely, and a new package version can keep using them.
TAG = "models-ppocrv5-1"
URL = "https://github.com/luukvisser/tidyscan/releases/download/{tag}/{name}"
TIMEOUT = 60  # seconds without data before a download is given up


class Asset(NamedTuple):
    path: str  # relative to the model directory
    sha256: str
    max_bytes: int  # a download larger than this is refused


# Release asset name: where it goes and what it must be. The models' hashes are those of
# tools/models.py's conversion (paddle2onnx 2.1.0 from the pinned PaddlePaddle commits). The models
# are Apache-2.0, which asks for the licence and a statement of the changes to travel with them:
# LICENSE-Apache-2.0.txt (Apache's own text) and MODELS-NOTICE.txt (sources, commits, conversion;
# tools/models.py writes it, and a test checks it still hashes to the value here).
ASSETS: dict[str, Asset] = {
    "PP-OCRv5_mobile_det.onnx": Asset(
        "PP-OCRv5_mobile_det/model.onnx",
        "d5de5df358366210d16419b9636a2fc1efa5d7a20688f38a7869ec7b1a4f4f7d",
        16 << 20,
    ),
    "latin_PP-OCRv5_mobile_rec.onnx": Asset(
        "latin_PP-OCRv5_mobile_rec/model.onnx",
        "995b0f5f28d2073896a78c03b5b863eae6af3744bafa0245b8522beea6994927",
        32 << 20,
    ),
    "latin_PP-OCRv5_mobile_rec.keys.txt": Asset(
        "latin_PP-OCRv5_mobile_rec/keys.txt",
        "ccbcc45730b3fbbd9050c5bc74db6a99067141ef1035e3d14889a84a6b9b1aff",
        1 << 20,
    ),
    "MODELS-NOTICE.txt": Asset(
        "MODELS-NOTICE.txt", "f79288ef80ea763e0131e8d4517599c83006d43a0119a87794e17c04fdd6fbd1", 1 << 20
    ),
    "LICENSE-Apache-2.0.txt": Asset(
        "LICENSE-Apache-2.0.txt",
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        1 << 20,
    ),
}
MODEL_FILES = ("PP-OCRv5_mobile_det.onnx", "latin_PP-OCRv5_mobile_rec.onnx", "latin_PP-OCRv5_mobile_rec.keys.txt")
DET, REC, KEYS = (ASSETS[name].path for name in MODEL_FILES)


class ModelError(Exception):
    pass


class _Response(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...
    def __enter__(self) -> _Response: ...
    def __exit__(
        self, typ: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None, /
    ) -> None: ...


def user_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "tidyscan", "models")


def complete(directory: str) -> bool:
    """True when the directory holds the three model files (their content is not checked)."""
    return all(os.path.isfile(os.path.join(directory, ASSETS[name].path)) for name in MODEL_FILES)


def model_dir() -> str:
    """The directory the paddle engine reads its models from (see the module docstring)."""
    env = os.environ.get(MODELS_ENV)
    if env:
        return env
    for candidate in (user_dir(), SYSTEM_DIR):
        if complete(candidate):
            return candidate
    return user_dir()


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _urlopen(url: str, timeout: float) -> _Response:
    return urllib.request.urlopen(url, timeout=timeout)


def fetch(directory: str | None = None, opener: Callable[[str, float], _Response] | None = None) -> list[str]:
    """Download the models into directory (default: $TIDYSCAN_PADDLE_MODELS or the per-user
    directory) and return the paths written. A file already there with the right sha256 is kept;
    one with another sha256 is replaced. Each download is checked before it takes the file's
    place, so a failed or wrong one leaves nothing behind. Raises ModelError."""
    directory = directory or os.environ.get(MODELS_ENV) or user_dir()
    opener = opener or _urlopen
    written = []
    for name, asset in ASSETS.items():
        target = os.path.join(directory, asset.path)
        if os.path.isfile(target) and sha256(target) == asset.sha256:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.{secrets.token_hex(4)}.part")
        url = URL.format(tag=TAG, name=name)
        try:
            h, size = hashlib.sha256(), 0
            with opener(url, TIMEOUT) as r, open(tmp, "xb") as f:
                while block := r.read(1 << 20):
                    size += len(block)
                    if size > asset.max_bytes:
                        raise ModelError(f"{name}: larger than {asset.max_bytes} bytes, refused")
                    h.update(block)
                    f.write(block)
            if h.hexdigest() != asset.sha256:
                raise ModelError(f"{name}: sha256 {h.hexdigest()}, expected {asset.sha256}; refused")
            os.replace(tmp, target)
        except (OSError, http.client.HTTPException) as e:  # IncompleteRead is not an OSError
            raise ModelError(f"{name}: {e}") from e
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        written.append(target)
    return written


def describe() -> str:
    """Where the models are looked up and what is there, for `tidyscan models path`."""
    lines = []
    env = os.environ.get(MODELS_ENV)
    if env:
        lines.append(f"${MODELS_ENV}: {env} ({'complete' if complete(env) else 'incomplete'})")
    for label, candidate in (("per-user", user_dir()), ("system", SYSTEM_DIR)):
        lines.append(f"{label}: {candidate} ({'complete' if complete(candidate) else 'missing'})")
    used = model_dir()
    lines.append(f"used: {used}" + ("" if complete(used) else "  (no models: run `tidyscan models fetch`)"))
    return "\n".join(lines)
