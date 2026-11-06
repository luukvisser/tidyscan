# The PaddleOCR models

PaddleOCR needs two models as ONNX files: PaddlePaddle's PP-OCRv5 mobile text
detector and Latin recogniser (Apache-2.0), converted to ONNX. tidyscan never
downloads them by itself.

## Fetching them

    tidyscan models fetch          # into the per-user directory
    tidyscan models fetch DIR      # into another directory
    tidyscan models path           # where they are looked up, and whether they are there

`fetch` takes them from this project's release `models-ppocrv5-1` and checks
each file's sha256 before it is put in place; a file that is already there
and correct is not downloaded again. The release's `MODELS-NOTICE.txt` says
from which commits they come and how they were converted.

They are looked up in `$TIDYSCAN_PADDLE_MODELS` when set, else in the per-user
directory (`$XDG_DATA_HOME/tidyscan/models`, by default
`~/.local/share/tidyscan/models`), else in `/opt/tidyscan/models` (where the
`paddle` image has them).

## Building them yourself

`tools/models.py` builds the same files from PaddlePaddle's Hugging Face
repositories: pinned commits, sha256-checked downloads, a pinned paddle2onnx,
and the result checked against the same hashes as `fetch` uses.

    pip install --no-deps --require-hashes -r tools/convert-requirements.txt   # Python 3.12, x86_64
    python3 -I tools/models.py ~/.local/share/tidyscan/models
