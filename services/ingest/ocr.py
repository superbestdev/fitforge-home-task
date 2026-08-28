"""OCR path for scanned manuals.

Shells out to ocrmypdf (MPL-2.0), which drives Tesseract and rewrites the PDF
with a searchable text layer. Doing it this way rather than calling Tesseract
directly buys us deskew, page-rotation detection and image cleanup for free —
all of which matter, because the scanned corpus is skewed and noisy by design.

Results are cached on disk by content hash. OCR is by far the slowest stage in
the pipeline and re-running ingestion during development should not re-pay it.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_DIR = Path("/app/data/ocr_cache")


@dataclass
class OcrResult:
    ok: bool
    output_path: Path | None
    cached: bool
    error: str | None = None
    duration_s: float = 0.0


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None and shutil.which("tesseract") is not None


def _cache_key(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with pdf_path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:32]


def ocr_pdf(pdf_path: Path, *, timeout_s: int = 600) -> OcrResult:
    """Add a text layer to a scanned PDF, returning the path to the new file."""
    import time

    if not ocr_available():
        return OcrResult(False, None, False, error="ocrmypdf/tesseract not installed")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"{_cache_key(pdf_path)}.pdf"
    if out_path.exists() and out_path.stat().st_size > 0:
        return OcrResult(True, out_path, cached=True)

    started = time.monotonic()
    cmd = [
        "ocrmypdf",
        "--force-ocr",          # the input has no text layer; do not skip pages
        "--deskew",             # our synthetic scans are rotated on purpose
        "--clean",              # unpaper: removes speckle before recognition
        "--optimize", "1",
        "--language", "eng",
        # Tesseract is single-threaded per page; ocrmypdf parallelises pages.
        "--jobs", "4",
        "--quiet",
        str(pdf_path),
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, text=True)
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        return OcrResult(False, None, False, error=f"OCR timed out after {timeout_s}s")

    duration = time.monotonic() - started

    if proc.returncode != 0 or not out_path.exists():
        out_path.unlink(missing_ok=True)
        err = (proc.stderr or proc.stdout or "unknown ocrmypdf failure").strip()[:500]
        return OcrResult(False, None, False, error=err, duration_s=duration)

    return OcrResult(True, out_path, cached=False, duration_s=duration)
