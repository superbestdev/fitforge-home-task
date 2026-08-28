"""Manual upload, model detection, and background ingestion.

This is the operational counterpart to the cold-start design: the coverage
registry tells you *which* models have no documentation, and this module is how
somebody fixes that. Uploading a manual runs exactly the same pipeline as the
seeded corpus — classify, OCR if needed, chunk, extract error codes, embed,
index, and update coverage — so an uploaded scan is treated with precisely the
same distrust as a seeded one.

Two things it does that a naive "save the file and index it" would not:

**It works out which model the manual belongs to.** Asking an uploader to pick
from 300 SKUs is how manuals end up filed against the wrong machine, and a
mis-filed manual is worse than a missing one — it poisons retrieval for a model
that then looks well-documented. The model number is printed in the manual, so
we read it.

**It runs in the background.** OCR on a scanned manual takes seconds to minutes.
The upload returns a job; the console watches it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..db import execute, query, query_one

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(settings.manuals_dir) / "uploads"
MAX_UPLOAD_BYTES = 60 * 1024 * 1024          # 60 MB; scanned manuals get large
PDF_MAGIC = b"%PDF-"


class UploadError(ValueError):
    """A problem with the upload itself, reported straight back to the user."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_pdf(filename: str, content: bytes) -> None:
    if not content:
        raise UploadError("That file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"That file is {len(content) / 1e6:.0f} MB. The limit is "
            f"{MAX_UPLOAD_BYTES // 1_000_000} MB — try splitting it, or "
            f"re-exporting at a lower scan resolution."
        )
    # Trust the bytes, not the extension.
    if not content.startswith(PDF_MAGIC):
        raise UploadError(
            "That does not look like a PDF. Service manuals must be uploaded as "
            "PDF; if you have a Word or InDesign source, export it to PDF first "
            "— the text layer will be far better than a scan."
        )


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    model_id: str | None
    confidence: float
    method: str
    candidates: list[dict]
    evidence: dict

    def to_json(self) -> dict:
        return {
            "model_id": self.model_id, "confidence": self.confidence,
            "method": self.method, "candidates": self.candidates,
            "evidence": self.evidence,
        }


# Model numbers look like FF-TT-PACER-350-PRO. Deliberately permissive about the
# tail so a manual for a variant still matches its base SKU.
MODEL_ID_RE = re.compile(r"\bFF-[A-Z]{2}-[A-Z0-9]+-\d{2,4}(?:-[A-Z]+)?\b")
SERIAL_PREFIX_RE = re.compile(r"\b([A-Z]{2,4}\d{2,4})\b")


def detect_model(pdf_path: Path, *, ocr_if_needed: bool = True) -> DetectionResult:
    """Work out which model a manual documents, from its own contents.

    Tried in order of reliability: an exact model number printed in the
    document, then the serial prefix it declares, then the product name. Each
    reports honest confidence, and anything ambiguous returns candidates for a
    human to choose between rather than guessing.
    """
    from services.ingest.extract import extract_pdf

    try:
        extracted = extract_pdf(str(pdf_path))
    except Exception as exc:                            # noqa: BLE001
        return DetectionResult(None, 0.0, "unreadable", [],
                               {"error": f"could not read the PDF: {exc}"})

    # Only the first few pages matter — identification lives at the front — and
    # OCRing a whole scanned manual just to read its cover would be wasteful.
    head = "\n".join(p.text for p in extracted.pages[:4])

    if len(head.strip()) < 80 and ocr_if_needed:
        head = _ocr_first_pages(pdf_path)

    if not head.strip():
        return DetectionResult(
            None, 0.0, "no_text", [],
            {"note": "No readable text on the first pages, even after OCR. "
                     "Pick the model manually."},
        )

    upper = head.upper()

    # --- 1. an explicit model number -------------------------------------
    for candidate_id in dict.fromkeys(MODEL_ID_RE.findall(upper)):
        row = query_one("SELECT id, name, category_id FROM models WHERE id = %s",
                        (candidate_id,))
        if row:
            return DetectionResult(
                row["id"], 0.99, "model_number", [dict(row)],
                {"matched": candidate_id},
            )

    # --- 2. the serial prefix the manual declares ------------------------
    prefixes = {p for p in SERIAL_PREFIX_RE.findall(upper)}
    if prefixes:
        rows = query(
            "SELECT id, name, category_id, serial_prefix FROM models "
            "WHERE serial_prefix = ANY(%s) LIMIT 6",
            (list(prefixes),),
        )
        if len(rows) == 1:
            return DetectionResult(
                rows[0]["id"], 0.93, "serial_prefix", [dict(rows[0])],
                {"matched": rows[0]["serial_prefix"]},
            )
        if rows:
            return DetectionResult(
                None, 0.5, "serial_prefix_ambiguous", [dict(r) for r in rows],
                {"matched": sorted(prefixes)},
            )

    # --- 3. the product name ---------------------------------------------
    rows = query(
        """
        SELECT id, name, category_id, similarity(upper(name), %(head)s) AS score
          FROM models
         WHERE upper(%(head)s) LIKE '%%' || upper(name) || '%%'
         ORDER BY length(name) DESC
         LIMIT 6
        """,
        {"head": upper[:2000]},
    )
    if len(rows) == 1:
        return DetectionResult(rows[0]["id"], 0.85, "product_name",
                               [dict(rows[0])], {"matched": rows[0]["name"]})
    if rows:
        return DetectionResult(None, 0.45, "product_name_ambiguous",
                               [dict(r) for r in rows], {})

    return DetectionResult(
        None, 0.0, "no_match", [],
        {"note": "Could not find a model number, serial prefix or product name "
                 "in this document."},
    )


def _ocr_first_pages(pdf_path: Path, pages: int = 3) -> str:
    """OCR just the front of a scanned manual, to read its cover."""
    from services.ingest.extract import extract_pdf
    from services.ingest.ocr import ocr_pdf

    result = ocr_pdf(pdf_path, timeout_s=180)
    if not result.ok or result.output_path is None:
        log.warning("cover OCR failed for %s: %s", pdf_path.name, result.error)
        return ""
    extracted = extract_pdf(str(result.output_path))
    return "\n".join(p.text for p in extracted.pages[:pages])


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def create_job(*, filename: str, content: bytes, model_id: str | None,
               uploaded_by: str | None = None) -> dict:
    """Store an uploaded manual and queue it for ingestion."""
    validate_pdf(filename, content)

    digest = content_hash(content)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:120] or "manual.pdf"
    stored = UPLOAD_DIR / f"{digest[:16]}__{safe}"
    stored.write_bytes(content)

    if model_id:
        exists = query_one("SELECT id FROM models WHERE id = %s", (model_id,))
        if not exists:
            raise UploadError(f"No such model: {model_id}")

    row = execute(
        """
        INSERT INTO ingest_jobs (model_id, filename, stored_path, size_bytes,
                                 content_hash, status, stage, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, 'queued', 'Queued', %s)
        RETURNING id
        """,
        (model_id, filename[:200], str(stored), len(content), digest, uploaded_by),
    )
    return {"job_id": str(row["id"]), "filename": filename,
            "size_bytes": len(content), "model_id": model_id}


def _update(job_id: str, **fields) -> None:
    if not fields:
        return
    sets, params = [], []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(json.dumps(value, default=str)
                      if key in ("detection", "result") else value)
    params.append(job_id)
    execute(f"UPDATE ingest_jobs SET {', '.join(sets)} WHERE id = %s", params)


def process_job(job_id: str) -> None:
    """Run one upload through detection and the full ingestion pipeline.

    Executed in a worker thread. Every failure path records a reason on the job
    rather than raising, because an upload that silently vanishes is the worst
    possible outcome for whoever is working through a backfill queue.
    """
    from services.ingest.pipeline import ingest_manual

    job = query_one("SELECT * FROM ingest_jobs WHERE id = %s", (job_id,))
    if job is None:
        log.warning("ingest job %s disappeared", job_id)
        return

    path = Path(job["stored_path"])
    _update(job_id, status="detecting", stage="Reading the document",
            started_at="now()")

    try:
        model_id = job["model_id"]

        # --- resolve the model -------------------------------------------
        if not model_id:
            _update(job_id, stage="Working out which model this is")
            detection = detect_model(path)
            _update(job_id, detection=detection.to_json())

            if detection.model_id is None:
                # Stop and ask rather than file it against a guess.
                _update(
                    job_id, status="awaiting_model",
                    stage="Could not identify the model — please choose one",
                )
                return
            model_id = detection.model_id
            _update(job_id, model_id=model_id)
        else:
            # The uploader named a model. Read the document anyway and compare:
            # a manual filed against the wrong SKU is worse than a missing one,
            # because that model then looks documented while every retrieval
            # against it returns another machine's procedures. The uploader's
            # choice still wins — they may be filing a manual whose cover is
            # wrong — but the disagreement is recorded and surfaced.
            _update(job_id, stage="Checking the document matches the model")
            detected = detect_model(path)
            detection = {"method": "chosen_by_uploader", "confidence": 1.0,
                         "model_id": model_id,
                         "detected_model_id": detected.model_id,
                         "detected_confidence": detected.confidence,
                         "detected_method": detected.method}

            if (detected.model_id and detected.model_id != model_id
                    and detected.confidence >= 0.85):
                detected_name = query_one(
                    "SELECT name FROM models WHERE id = %s", (detected.model_id,)
                )
                detection["mismatch"] = (
                    f"This document identifies itself as "
                    f"{detected.model_id} ({(detected_name or {}).get('name')}), "
                    f"but you filed it under {model_id}. It has been indexed as "
                    f"you asked — check this is right."
                )
                log.warning("job %s: model mismatch, document says %s, filed as %s",
                            job_id, detected.model_id, model_id)
            _update(job_id, detection=detection)

        # --- replace any existing manual for this model -------------------
        # A model has one current manual. Superseding rather than accumulating
        # keeps retrieval from mixing two revisions of the same document.
        previous = query(
            "SELECT id FROM manuals WHERE model_id = %s", (model_id,)
        )
        for prev in previous:
            execute("DELETE FROM doc_chunks WHERE manual_id = %s", (prev["id"],))
        # The coverage row references the manual, so its pointer has to be
        # released before the old manual can go. The row itself is kept and
        # rewritten by the ingest, which keeps the model's coverage status
        # continuously present rather than briefly vanishing mid-replacement.
        execute(
            "UPDATE coverage_registry SET manual_id = NULL WHERE model_id = %s",
            (model_id,),
        )
        execute("DELETE FROM manuals WHERE model_id = %s", (model_id,))
        execute("DELETE FROM error_codes WHERE model_id = %s", (model_id,))

        # --- register and ingest ------------------------------------------
        source_type = _classify_source(path)
        _update(job_id, status="ingesting",
                stage=("Running OCR — this can take a minute"
                       if source_type == "scanned" else "Extracting and indexing"))

        manual_row = execute(
            """
            INSERT INTO manuals (model_id, path, source_type, uploaded,
                                 original_filename)
            VALUES (%s, %s, %s, TRUE, %s)
            RETURNING id
            """,
            (model_id, str(path), source_type, job["filename"]),
        )
        manual_id = manual_row["id"]

        outcome = ingest_manual(
            {"id": manual_id, "model_id": model_id, "path": str(path),
             "source_type": source_type},
            reingest=True,
        )

        result = {
            "status": outcome.status,
            "chunks": outcome.chunks,
            "error_codes": outcome.error_codes,
            "confidence": outcome.confidence,
            "ocr_used": outcome.ocr_used,
            "sections": outcome.sections,
            "duration_s": outcome.duration_s,
        }
        coverage = query_one(
            "SELECT status, quality_score, notes FROM coverage_registry "
            "WHERE model_id = %s", (model_id,)
        )
        if coverage:
            result["coverage"] = dict(coverage)

        if outcome.status in ("ok", "degraded"):
            _update(job_id, status="done", manual_id=manual_id,
                    stage=f"Indexed {outcome.chunks} sections",
                    result=result, finished_at="now()")
        else:
            _update(job_id, status="failed", manual_id=manual_id,
                    stage="Ingestion failed",
                    error=outcome.error or "The document produced no usable content.",
                    result=result, finished_at="now()")

    except Exception as exc:                            # noqa: BLE001
        log.exception("ingest job %s failed", job_id)
        _update(job_id, status="failed", stage="Failed",
                error=str(exc)[:500], finished_at="now()")


def _classify_source(path: Path) -> str:
    """born_digital or scanned, by how much text the pages actually carry."""
    from services.ingest.extract import extract_pdf

    try:
        extracted = extract_pdf(str(path))
    except Exception:                                   # noqa: BLE001
        return "scanned"
    return "scanned" if extracted.needs_ocr else "born_digital"


def assign_model(job_id: str, model_id: str) -> None:
    """Set the model on a job that stalled at `awaiting_model`, and resume it."""
    exists = query_one("SELECT id FROM models WHERE id = %s", (model_id,))
    if not exists:
        raise UploadError(f"No such model: {model_id}")
    _update(job_id, model_id=model_id, status="queued", stage="Queued",
            error=None)


def get_job(job_id: str) -> dict | None:
    row = query_one(
        """
        SELECT j.*, m.name AS model_name
          FROM ingest_jobs j LEFT JOIN models m ON m.id = j.model_id
         WHERE j.id = %s
        """,
        (job_id,),
    )
    return dict(row) if row else None


def list_jobs(limit: int = 30) -> list[dict]:
    return [dict(r) for r in query(
        """
        SELECT j.id, j.model_id, m.name AS model_name, j.filename, j.status,
               j.stage, j.error, j.result, j.detection, j.size_bytes,
               j.created_at, j.finished_at
          FROM ingest_jobs j LEFT JOIN models m ON m.id = j.model_id
         ORDER BY j.created_at DESC LIMIT %s
        """,
        (limit,),
    )]


def backfill_queue(limit: int = 100) -> list[dict]:
    """Models with no usable documentation — the work list for uploads.

    Ordered by how much support traffic each model actually generates, because
    "17 models are unbacked" is not actionable and "these three account for 6%
    of your sessions" is.
    """
    return [dict(r) for r in query(
        """
        SELECT c.model_id, m.name, m.category_id, c.status, c.quality_score,
               c.notes, mn.source_type,
               COALESCE(t.sessions, 0) AS sessions
          FROM coverage_registry c
          JOIN models m ON m.id = c.model_id
          LEFT JOIN manuals mn ON mn.id = c.manual_id
          LEFT JOIN (
              SELECT model_id, count(DISTINCT session_id) AS sessions
                FROM issue_threads WHERE model_id IS NOT NULL
               GROUP BY model_id
          ) t ON t.model_id = c.model_id
         WHERE c.status IN ('unbacked', 'degraded')
         ORDER BY sessions DESC, c.quality_score ASC, m.id
         LIMIT %s
        """,
        (limit,),
    )]


def search_models(q: str, limit: int = 20) -> list[dict]:
    """Model picker for the console."""
    term = (q or "").strip()
    if not term:
        return [dict(r) for r in query(
            "SELECT id, name, category_id FROM models ORDER BY id LIMIT %s", (limit,)
        )]
    return [dict(r) for r in query(
        """
        SELECT m.id, m.name, m.category_id, c.status AS coverage_status
          FROM models m
          LEFT JOIN coverage_registry c ON c.model_id = m.id
         WHERE m.id ILIKE %(q)s OR m.name ILIKE %(q)s
         ORDER BY similarity(m.name, %(raw)s) DESC, m.id
         LIMIT %(limit)s
        """,
        {"q": f"%{term}%", "raw": term, "limit": limit},
    )]
