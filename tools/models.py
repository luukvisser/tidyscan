"""Fetch PaddleOCR's PP-OCRv5 models from Hugging Face, check them, convert them to ONNX.

    python3 -I tools/models.py OUTDIR                 the model directory, laid out as tidyscan reads it
    python3 -I tools/models.py --release OUTDIR DIST  also the release assets in DIST: the files under
                                                      their asset names, SHA256SUMS, MODELS-NOTICE.txt

Each file is fetched from a fixed commit of PaddlePaddle's own repository and checked against its
sha256 before use; the converted ONNX files and the recognizer's character list are checked too
(paddle2onnx output is reproducible for a given converter version), so a changed input or converter
fails instead of producing a different model. The converted files' names and hashes come from
src/tidyscan/models.py, the same values `tidyscan models fetch` checks a download against.
Needs the pinned converter: pip install --no-deps --require-hashes -r tools/convert-requirements.txt
(CPython 3.12, x86_64). Licence of the models: Apache-2.0.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import urllib.request
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from types import ModuleType

HERE = os.path.dirname(os.path.abspath(__file__))
HF = "https://huggingface.co/PaddlePaddle/{repo}/resolve/{rev}/{name}"
APACHE = os.path.join(HERE, "models-licence-apache-2.0.txt")  # Apache's own text, checked by sha256
PADDLE2ONNX = "paddle2onnx 2.1.0"


def _load_models_module() -> ModuleType:
    """src/tidyscan/models.py, from its path: the package itself is not installed here."""
    path = os.path.join(HERE, os.pardir, "src", "tidyscan", "models.py")
    spec = importlib.util.spec_from_file_location("tidyscan_models", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the NamedTuple it defines looks its module up here
    spec.loader.exec_module(module)
    return module


M = _load_models_module()


class Model(TypedDict):
    rev: str  # the commit of PaddlePaddle's repository
    files: dict[str, str]  # file name: sha256, as fetched


SOURCES: dict[str, Model] = {
    "PP-OCRv5_mobile_det": {
        "rev": "0d63e78e2b680928f6b1747d76a08db6e645efb7",
        "files": {
            "inference.json": "05feef1acb00aa4cd7362b15f7f501fc4f99d7b1fa73c1c871e0c7b1504b0f5c",
            "inference.pdiparams": "afa1820cb16c1fd0dad589d0f8b389139061c1ef6d68019685fd07be997dda5b",
            "inference.yml": "98069072e1b6b37d727fd9d9f11725faa46d6ea0de012f2ed26caea011c37699",
        },
    },
    "latin_PP-OCRv5_mobile_rec": {
        "rev": "ab2cd5cc5fa6309be2e5acdfe66eca2c2c127d57",
        "files": {
            "inference.json": "820b2ee8e5072de5156c3d55124c2b03c82ef278d59f453f857c0789bacb4da1",
            "inference.pdiparams": "53cdc8b481a7394bb108f96d0fb3432b0a8f392e22c7d18f06dbb2d42b8b25f9",
            "inference.yml": "0bbe984570f597af3638e50bdf2e8276f3ab26a61966096538b3b0d1849f5c84",
        },
    },
}


def outputs(repo: str) -> dict[str, str]:
    """The converted files of one model: name in its directory: sha256 (from models.ASSETS)."""
    prefix = repo + "/"
    return {a.path[len(prefix) :]: a.sha256 for a in M.ASSETS.values() if a.path.startswith(prefix)}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check(path: str, want: str) -> None:
    got = sha256(path)
    if got != want:
        sys.exit(f"{path}: sha256 {got}, expected {want}")
    print(f"ok  {want[:16]}  {path}", flush=True)


def build(out: str) -> None:
    src = out.rstrip("/") + "-src"  # the Paddle originals, not shipped
    for repo, m in SOURCES.items():
        sdir, odir = os.path.join(src, repo), os.path.join(out, repo)
        os.makedirs(sdir, exist_ok=True)
        os.makedirs(odir, exist_ok=True)
        for name, digest in m["files"].items():
            path = os.path.join(sdir, name)
            with (
                urllib.request.urlopen(HF.format(repo=repo, rev=m["rev"], name=name), timeout=120) as r,
                open(path, "wb") as f,
            ):
                while block := r.read(1 << 20):
                    f.write(block)
            check(path, digest)
        subprocess.run(
            [
                "paddle2onnx",
                "--model_dir",
                sdir,
                "--model_filename",
                "inference.json",
                "--params_filename",
                "inference.pdiparams",
                "--opset_version",
                "14",
                "--save_file",
                os.path.join(odir, "model.onnx"),
            ],
            check=True,
        )
        wanted = outputs(repo)
        if "keys.txt" in wanted:
            import yaml  # only here: the notice and the tests need no PyYAML

            with open(os.path.join(sdir, "inference.yml"), encoding="utf-8") as f:
                chars = yaml.safe_load(f)["PostProcess"]["character_dict"]
            with open(os.path.join(odir, "keys.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(chars) + "\n")
        for name, digest in wanted.items():
            check(os.path.join(odir, name), digest)
    licence = M.ASSETS["LICENSE-Apache-2.0.txt"]
    check(APACHE, licence.sha256)
    shutil.copyfile(APACHE, os.path.join(out, licence.path))
    with open(os.path.join(out, "MODELS-NOTICE.txt"), "w", encoding="utf-8") as f:
        f.write(notice())
    check(os.path.join(out, "MODELS-NOTICE.txt"), M.ASSETS["MODELS-NOTICE.txt"].sha256)
    with open(os.path.join(out, "SOURCE.txt"), "w") as f:
        f.write(
            "PaddleOCR PP-OCRv5 models (Apache-2.0, https://github.com/PaddlePaddle/PaddleOCR),\n"
            f"converted to ONNX with {PADDLE2ONNX}; licence in {licence.path}, see MODELS-NOTICE.txt.\n"
        )
        for repo, m in SOURCES.items():
            f.write(f"{repo}: https://huggingface.co/PaddlePaddle/{repo} at {m['rev']}\n")
            for name, digest in {**m["files"], **outputs(repo)}.items():
                f.write(f"  {digest}  {name}\n")


def notice() -> str:
    """MODELS-NOTICE.txt: where the models come from and how they were changed (Apache-2.0 4(b))."""
    lines = [
        "PaddleOCR PP-OCRv5 models, converted to ONNX for tidyscan",
        "",
        "These files are PaddlePaddle's PP-OCRv5 text detection and Latin text recognition",
        "models (https://github.com/PaddlePaddle/PaddleOCR), licensed under the Apache License,",
        "Version 2.0 (LICENSE-Apache-2.0.txt, next to this file). They were taken from",
        "PaddlePaddle's own Hugging Face repositories, which carry no NOTICE file,",
        "at these commits:",
        "",
    ]
    lines += [f"  {repo}: https://huggingface.co/PaddlePaddle/{repo} at {m['rev']}" for repo, m in SOURCES.items()]
    lines += [
        "",
        f"Changes: converted from Paddle inference format to ONNX with {PADDLE2ONNX} (opset 14);",
        "the weights are unchanged. keys.txt is the recogniser's character list, taken from",
        "inference.yml (PostProcess.character_dict), one character per line.",
        "",
        "Asset, sha256:",
        "",
    ]
    lines += [f"  {name}  {M.ASSETS[name].sha256}" for name in M.MODEL_FILES]
    return "\n".join(lines) + "\n"


def release(out: str, dist: str) -> None:
    """The release assets, from a model directory build() made and checked."""
    os.makedirs(dist, exist_ok=True)
    sums = []
    for name, a in M.ASSETS.items():
        src = os.path.join(out, a.path)
        check(src, a.sha256)
        shutil.copyfile(src, os.path.join(dist, name))
        sums.append(f"{a.sha256}  {name}")
    with open(os.path.join(dist, "SHA256SUMS"), "w") as f:
        f.write("\n".join(sums) + "\n")
    print(f"release assets for {M.TAG} in {dist}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--release"] and len(args) == 3:
        build(args[1])
        release(args[1], args[2])
    elif len(args) == 1 and not args[0].startswith("-"):
        build(args[0])
    else:
        sys.exit(__doc__)
