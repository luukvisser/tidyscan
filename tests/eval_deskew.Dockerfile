# The tools tests/eval_deskew.py compares, in one image (not part of the package or of CI):
#
#   docker build -f tests/eval_deskew.Dockerfile -t tidyscan-deskew-bench tests
#
# Debian trixie (the same base as tidyscan's full image): Tesseract 5.5 with osd/eng/nld,
# OCRmyPDF 16.7, Leptonica 1.84 (libleptonica6, for pixFindSkew through ctypes), unpaper 7,
# ImageMagick 7, poppler-utils (pdftoppm, to render the source PDFs). PyPI, pinned with hashes:
# deskew, jdeskew, rapid-orientation and their dependencies (tests/eval_deskew-requirements.txt).
# tidyscan itself is not installed: eval_deskew.py runs the checkout it lives in (mount it).
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ocrmypdf tesseract-ocr \
      tesseract-ocr-osd tesseract-ocr-eng tesseract-ocr-nld libleptonica6 unpaper imagemagick \
      poppler-utils time \
 && rm -rf /var/lib/apt/lists/*
COPY eval_deskew-requirements.txt /build/
# --system-site-packages: OCRmyPDF and pikepdf come from Debian; the PyPI wheels shadow the rest.
# --ignore-requires-python: rapid-orientation 0.0.11 declares Python <3.13; it is pure Python and runs.
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps --require-hashes --ignore-requires-python -r /build/eval_deskew-requirements.txt \
 && /opt/venv/bin/python3 -c "import cv2, deskew, jdeskew, rapid_orientation, skimage, ocrmypdf"
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 OMP_THREAD_LIMIT=2
RUN /usr/sbin/useradd --uid 1000 --create-home bench
USER 1000:1000
WORKDIR /home/bench
