# tidyscan, three images (tidyscan watch IN OUT: document folders into PDFs):
#   docker build --target base   -t tidyscan:base .    pages to PDF only: standard library, ~77 MB,
#                                                          no copyleft software (turning, straightening
#                                                          and OCR switch themselves off)
#   docker build -t tidyscan .   (--target full)       plus Tesseract and OCRmyPDF (Debian): pages
#                                                          upright and straight, a text layer (~480 MB)
#   docker build --target paddle -t tidyscan:paddle .  plus PaddleOCR PP-OCRv5 on OpenVINO (CPU), for
#                                                          receipts (--engine paddle; ~1 GB, amd64 only);
#                                                          only on request, never by default
# OCR languages: --build-arg OCR_LANGS="eng nld deu fra" (Tesseract language codes).

# Pure Python, so it is built on the build machine for every target platform.
FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS wheel
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip wheel --no-deps --no-cache-dir --wheel-dir /wheels /src

# --- base: no copyleft software -------------------------------------------------------------------
# The same Python on an empty image as scankeyd's (github.com/luukvisser/scankeyd): python-build-
# standalone's musl build, musl's loader, the tzdata wheel; no shell, no package manager.

# python-build-standalone release 20261013, CPython 3.13.16; the sha256 values are the release's
# SHA256SUMS. The runtime comes from the install_only build; the licence texts of its components
# only ship in the "full" build of the same release, so that is fetched too, for licenses/ alone.
FROM --platform=$BUILDPLATFORM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS python
ARG TARGETARCH
# DL3018: apk packages are not pinned by version (only zstd, for unpacking; nothing of it ships).
# hadolint ignore=DL3018
RUN apk add --no-cache zstd
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN set -eu; \
    pbs=https://github.com/astral-sh/python-build-standalone/releases/download/20261013; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64; \
             run_sha=a177ce931ddc616c0e76eebc6a583196739dc704d47af853f5958cb95b2b1c96; \
             full_sha=cfbe69fc57b40fa93b459aff97066d8c516c07ce6e4938eb5001640e542e7a54 ;; \
      arm64) arch=aarch64; \
             run_sha=9f503edbbfe384c9bf770d99ce787f7aea60bbc6a96ffb3b5933f72c9e71fb6f; \
             full_sha=cdfb095bb5f532bba2dbb17500fa60509d38e15678c72d227ae4e32ce5a6e74b ;; \
      *) echo "unsupported platform: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    name="cpython-3.13.16%2B20261013-$arch-unknown-linux-musl"; \
    wget -q -O /tmp/run.tgz "$pbs/$name-install_only_stripped.tar.gz"; \
    echo "$run_sha  /tmp/run.tgz" | sha256sum -c -; \
    wget -q -O /tmp/full.tar.zst "$pbs/$name-lto-full.tar.zst"; \
    echo "$full_sha  /tmp/full.tar.zst" | sha256sum -c -; \
    mkdir -p /opt /out/licenses/python; \
    tar -xzf /tmp/run.tgz -C /opt; \
    mkdir -p /tmp/full; zstd -dc /tmp/full.tar.zst | tar -x -C /tmp/full; \
    cp /tmp/full/python/licenses/* /out/licenses/python/; \
    cp /opt/python/lib/python3.13/LICENSE.txt /out/licenses/python/LICENSE.cpython-distribution.txt; \
    l=/out/licenses/python; \
    rm -f $l/LICENSE.bdb.txt $l/LICENSE.tcl.txt $l/LICENSE.tix.txt $l/LICENSE.libX11.txt \
          $l/LICENSE.libXau.txt $l/LICENSE.libxcb.txt; \
    p=/opt/python; y=$p/lib/python3.13; \
    rm -rf $p/lib/libtcl* $p/lib/itcl* $p/lib/tcl* $p/lib/tk* $p/lib/thread* $p/lib/pkgconfig \
      $p/include $p/share $y/lib-dynload/_dbm.* $y/lib-dynload/_tkinter.* $y/tkinter $y/idlelib \
      $y/turtledemo $y/turtle.py $y/dbm/ndbm.py $y/ensurepip $y/test $y/lib2to3 \
      $y/site-packages/pip $y/site-packages/pip-* $y/config-3.13-*-linux-musl \
      $p/bin/idle3* $p/bin/pip* $p/bin/2to3*; \
    find /opt/python -name __pycache__ -prune -exec rm -rf {} +; \
    rm -rf /tmp/run.tgz /tmp/full.tar.zst /tmp/full

# musl's licence text, from the source release of the version in the Alpine image below (1.2.5;
# checked against musl's signing key when pinned).
FROM --platform=$BUILDPLATFORM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS musl-licence
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN wget -q -O /tmp/musl.tgz https://musl.libc.org/releases/musl-1.2.5.tar.gz \
 && echo "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4  /tmp/musl.tgz" | sha256sum -c - \
 && mkdir -p /out/licenses/musl \
 && tar -xzf /tmp/musl.tgz -O musl-1.2.5/COPYRIGHT > /out/licenses/musl/COPYRIGHT \
 && rm /tmp/musl.tgz

# The target platform's Alpine, for musl's dynamic loader only (no RUN: nothing is executed in it).
FROM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS musl-loader

# tidyscan itself, the time zone data, the users and the folders.
FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS site
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
RUN --mount=from=wheel,source=/wheels,target=/wheels \
    pip download --no-deps --no-cache-dir --dest /tzwheel "tzdata==2026.5" \
 && echo "b683bd1b6659ddcd810ff02ad09ba821d4bf1065072805063eb35c49617905ac  /tzwheel/tzdata-2026.5-py2.py3-none-any.whl" | sha256sum -c - \
 && pip install --no-deps --no-cache-dir --no-compile --target /site /wheels/tidyscan-*.whl \
 && python3 -c "import zipfile; zipfile.ZipFile('/tzwheel/tzdata-2026.5-py2.py3-none-any.whl').extractall('/tz')" \
 && mkdir -p /root-fs/usr/share/licenses/tzdata /root-fs/etc /root-fs/in /root-fs/out /root-fs/tmp \
 && cp -r /tz/tzdata/zoneinfo /root-fs/usr/share/zoneinfo \
 && cp /tz/tzdata-2026.5.dist-info/licenses/LICENSE /root-fs/usr/share/licenses/tzdata/LICENSE \
 && cp /tz/tzdata-2026.5.dist-info/licenses/licenses/LICENSE_APACHE /root-fs/usr/share/licenses/tzdata/LICENSE_APACHE \
 && printf 'root:x:0:0::/:/nonexistent\ntidyscan:x:1000:1000::/tmp:/nonexistent\n' > /root-fs/etc/passwd \
 && printf 'root:x:0:\ntidyscan:x:1000:\n' > /root-fs/etc/group \
 && chown 1000:1000 /root-fs/in /root-fs/out && chmod 1777 /root-fs/tmp

FROM scratch AS base
COPY --from=musl-loader /lib/ld-musl-*.so.1 /lib/
COPY --from=python /opt/python /opt/python
COPY --from=python /out/ /usr/share/
COPY --from=musl-licence /out/ /usr/share/
COPY --from=site /root-fs/ /
COPY --from=site /site /opt/site
COPY LICENSE /usr/share/licenses/tidyscan/
ENV PATH=/opt/python/bin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/site
USER 1000:1000
ENTRYPOINT ["/opt/python/bin/python3", "-m", "tidyscan"]
CMD ["watch", "/in", "/out"]

# --- full: Tesseract and OCRmyPDF from Debian, with its security updates --------------------------
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS tesseract
ARG OCR_LANGS="eng nld deu fra"
# DL3008: Debian packages are not pinned by version; the base image is pinned by digest, and a
# pinned Debian version disappears from the mirrors with its next security update.
# SC2086: $OCR_LANGS is split into words on purpose.
# hadolint ignore=DL3008,SC2086
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pil python3-installer ocrmypdf \
      tesseract-ocr tesseract-ocr-osd $(for l in $OCR_LANGS; do echo tesseract-ocr-$l; done) \
 && rm -rf /var/lib/apt/lists/*
# A venv on top of Debian's packages (OCRmyPDF, Pillow, pikepdf from Debian) holds tidyscan.
# /opt/venv/bin/ocrmypdf: Debian's launcher, run by the venv's Python, so OCRmyPDF finds the plugin.
RUN --mount=from=wheel,source=/wheels,target=/wheels \
    python3 -m venv --system-site-packages --without-pip /opt/venv \
 && /opt/venv/bin/python3 -m installer /wheels/tidyscan-*.whl \
 && sed '1s|.*|#!/opt/venv/bin/python3|' /usr/bin/ocrmypdf > /opt/venv/bin/ocrmypdf && chmod 755 /opt/venv/bin/ocrmypdf \
 && useradd --uid 1000 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin tidyscan \
 && mkdir -p /in /out && chown 1000:1000 /in /out
ENV PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python3", "-m", "tidyscan"]
CMD ["watch", "/in", "/out"]

# --- paddle: full plus PaddleOCR PP-OCRv5 on OpenVINO (CPU) ---------------------------------------
# The runtime wheels, each checked against its sha256 (Python 3.13, as Debian trixie's).
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS paddle-wheels
COPY tools/paddle-requirements.txt /build/
RUN pip download --no-deps --require-hashes --no-cache-dir --only-binary :all: --dest /pwheels \
      -r /build/paddle-requirements.txt

# The models from PaddlePaddle's Hugging Face repositories at fixed commits, checked against their
# sha256, converted to ONNX with a pinned paddle2onnx and checked again (tools/models.py). Python
# 3.12: paddle2onnx 2.1 has no wheels for 3.13. Nothing of this stage but /models ships.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS models
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY tools/convert-requirements.txt /build/tools/
RUN pip install --no-deps --require-hashes --no-cache-dir -r /build/tools/convert-requirements.txt
COPY tools /build/tools
COPY src/tidyscan/models.py /build/src/tidyscan/models.py
RUN python3 -I /build/tools/models.py /models

FROM tesseract AS paddle
USER 0
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-numpy python3-shapely python3-pyclipper python3-yaml \
      python3-omegaconf python3-tqdm python3-requests python3-colorlog python3-six \
 && rm -rf /var/lib/apt/lists/*
# Removed after installing: RapidOCR's bundled default models (tidyscan names its own), OpenCV's
# Haar cascades, and OpenVINO's GPU and NPU plugins and its frontends other than ONNX and IR: ONNX
# models on the CPU only, and the iGPU cannot be touched.
# DL3003: the cd is to a glob path inside one RUN. SC2046: the loop's output is split on purpose.
# hadolint ignore=DL3003,SC2046
RUN --mount=from=paddle-wheels,source=/pwheels,target=/pwheels \
    for w in /pwheels/*.whl; do /opt/venv/bin/python3 -m installer "$w" || exit 1; done \
 && cd /opt/venv/lib/python3*/site-packages && rm -rf rapidocr/models/*.onnx cv2/data \
      openvino/libs/libopenvino_intel_gpu_plugin.so openvino/libs/libopenvino_intel_npu_plugin.so \
      openvino/libs/cache.json \
      $(for f in gguf jax paddle pytorch tensorflow tensorflow_lite; do echo openvino/libs/libopenvino_${f}_frontend.so.*; done) \
 && cd / \
 && /opt/venv/bin/python3 -c "import cv2, rapidocr, tidyscan.ocrmypdf_paddle, openvino as ov; \
assert ov.Core().available_devices == ['CPU'], ov.Core().available_devices"
COPY --from=models /models /opt/tidyscan/models
ENV TIDYSCAN_PADDLE_MODELS=/opt/tidyscan/models
USER 1000:1000

# The default target (a plain `docker build .`): full, without PaddleOCR. The paddle image is only
# built when asked for, with --target paddle.
FROM tesseract AS full
