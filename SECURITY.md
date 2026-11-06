# Security

## Reporting

Please report a vulnerability through GitHub's private vulnerability reporting
(the repository's Security tab), not in a public issue.

## What tidyscan does with its input

tidyscan reads page images (and document.json files) that it did not make,
so it treats them as untrusted:

- The PDF is written without decoding the pages. Turning and straightening
  read them with Pillow (with its decompression-bomb limit) and Tesseract; a
  page beyond 200 million pixels (an A4 page at 1200 dpi has 140 million) is
  not read at all, and Tesseract gets at most 5 minutes per page.
- Tesseract and OCRmyPDF run as separate processes, started with an argument
  list (never through a shell) and with a timeout.
- OCRmyPDF in turn runs Ghostscript on the PDF. Ghostscript has had serious
  vulnerabilities; if the pages come from people you do not trust, run OCR in
  a container or another sandbox, with no network and only the folders it
  needs.
- Nothing is sent anywhere: no network access while pages are processed, no
  telemetry, no update checks.

## Supply chain

- The PaddleOCR models are downloaded only by an explicit command
  (`tidyscan models fetch`), from this project's own release, and every
  file is checked against the sha256 in the code before it is put in place.
  `tools/models.py` builds them from PaddlePaddle's repositories at fixed
  commits, checked by sha256 as well.
- Releases to PyPI are built and uploaded by GitHub Actions with trusted
  publishing; no upload token exists.
- Actions in the workflows are pinned by commit.
