# Changelog

## 0.3.0 (unreleased)

First public release.

- **Upright**: Tesseract's orientation detection, used only with enough
  confidence and an allowed script; an unsure upside-down page, or a page
  that lies sideways, is settled by reading it both ways round.
- **Straight**: skew up to 45 degrees, from coarse projection candidates and
  Tesseract's text-line baselines, checked by measuring again; at most 5
  minutes of Tesseract per page.
- **Lossless PDF**: JPEG pages as they are, 1-bit pages Flate-compressed;
  turns as `/Rotate`, skew as a drawing matrix.
- **OCR** through OCRmyPDF: Tesseract by default, PaddleOCR PP-OCRv5 on
  OpenVINO (CPU) for receipts, photo captions and tables; the OCRmyPDF plugin
  `tidyscan.ocrmypdf_paddle` works with OCRmyPDF 16 and 17. PDF/A-2b on
  request.
- `tidyscan models fetch`: the PaddleOCR models from this project's release,
  checked by sha256.
- `tidyscan watch IN OUT`: document folders (as scankeyd writes them) into
  PDFs, as a service, with settings from the environment and a status file
  for a monitor.
- Container images: `base` (pages to PDF, standard library), the default
  `full` (Tesseract and OCRmyPDF) and, on request, `paddle`.
