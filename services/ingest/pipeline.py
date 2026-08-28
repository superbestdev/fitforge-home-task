"""Manual ingestion pipeline.

    classify -> OCR (if needed) -> extract -> chunk -> symbolic extraction
             -> embed -> index -> coverage registry

Run it with:

    python -m services.ingest.pipeline                 # everything not yet done
    python -m services.ingest.pipeline --model FF-...  # one model
    python -m services.ingest.pipeline --reingest      # rebuild from scratch

The coverage registry update at the end is not bookkeeping — it is what lets the
agent answer "do I actually have documentation for this machine?" before it
starts troubleshooting. See docs/08-cold-start.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from services.api.app.db import execute, execute_many, query

from .chunk import chunk_document, extract_error_codes
from .embed import embed_texts
from .extract import extract_pdf, text_quality
from .ocr import ocr_pdf

log = logging.getLogger(__name__)

# Sections we expect a complete manual to have. Missing troubleshooting or
# error codes is what separates "degraded" from "backed".
CRITICAL_SECTIONS = {"troubleshooting", "error_codes", "parts"}
EXPECTED_SECTIONS = CRITICAL_SECTIONS | {"identification", "safety", "maintenance", "warranty"}


@dataclass
class IngestOutcome:
    model_id: str
    manual_id: int
    source_type: str
    status: str                   # ok | degraded | failed | skipped
    chunks: int
    error_codes: int
    confidence: float
    ocr_used: bool
    sections: list[str]
    duration_s: float
    error: str | None = None


def ingest_manual(manual: dict, *, reingest: bool = False) -> IngestOutcome:
    started = time.monotonic()
    manual_id = manual["id"]
    model_id = manual["model_id"]
    source_type = manual["source_type"]

    def done(**kw) -> IngestOutcome:
        return IngestOutcome(
            model_id=model_id, manual_id=manual_id, source_type=source_type,
            duration_s=round(time.monotonic() - started, 2), **kw
        )

    # --- print-only / missing -------------------------------------------
    # There is nothing to ingest and that is a first-class outcome, not an
    # error. The registry records the gap so the agent knows it is blind.
    if source_type in ("print_only", "missing") or not manual.get("path"):
        _write_coverage(model_id, None, status="unbacked", chunk_count=0,
                        quality=0.0, sections=[],
                        notes="No digital manual available for this model.")
        _mark_manual(manual_id, confidence=0.0, ocr=False,
                     error="no digital source")
        return done(status="skipped", chunks=0, error_codes=0,
                    confidence=0.0, ocr_used=False, sections=[])

    path = Path(manual["path"])
    if not path.exists():
        _write_coverage(model_id, manual_id, status="unbacked", chunk_count=0,
                        quality=0.0, sections=[],
                        notes=f"Manual file missing at {path}")
        _mark_manual(manual_id, confidence=0.0, ocr=False, error="file not found")
        return done(status="failed", chunks=0, error_codes=0, confidence=0.0,
                    ocr_used=False, sections=[], error="file not found")

    if reingest:
        execute("DELETE FROM doc_chunks WHERE manual_id = %s", (manual_id,))
        execute("DELETE FROM error_codes WHERE model_id = %s", (model_id,))

    # --- 1. extract & classify ------------------------------------------
    result = extract_pdf(str(path))
    ocr_used = False

    # --- 2. OCR when there is no usable text layer ----------------------
    if result.needs_ocr:
        log.info("%s: %d/%d pages have no text layer -> OCR",
                 model_id, result.image_only_pages, result.page_count)
        ocr = ocr_pdf(path)
        if not ocr.ok or ocr.output_path is None:
            _write_coverage(model_id, manual_id, status="unbacked", chunk_count=0,
                            quality=0.0, sections=[],
                            notes=f"OCR failed: {ocr.error}")
            _mark_manual(manual_id, confidence=0.0, ocr=True, error=ocr.error)
            return done(status="failed", chunks=0, error_codes=0, confidence=0.0,
                        ocr_used=True, sections=[], error=ocr.error)
        result = extract_pdf(str(ocr.output_path))
        ocr_used = True
        log.info("%s: OCR complete in %.1fs (cached=%s)",
                 model_id, ocr.duration_s, ocr.cached)

    # --- 3. score the text we ended up with -----------------------------
    quality = text_quality(result.full_text)
    # OCR'd text is penalised even when it scores well. It is measurably worse
    # than a native text layer in ways this cheap heuristic cannot see, and the
    # penalty is what makes the agent lean on citations for these models.
    confidence = round(quality * (0.8 if ocr_used else 1.0), 3)

    # --- 4. chunk --------------------------------------------------------
    chunks = chunk_document(result.pages, result.full_text)
    if not chunks:
        _write_coverage(model_id, manual_id, status="unbacked", chunk_count=0,
                        quality=confidence, sections=[],
                        notes="Extraction produced no usable chunks.")
        _mark_manual(manual_id, confidence=confidence, ocr=ocr_used,
                     error="no chunks produced")
        return done(status="failed", chunks=0, error_codes=0, confidence=confidence,
                    ocr_used=ocr_used, sections=[], error="no chunks produced")

    # A chunk carrying an injection payload is never indexed. Dropping it is the
    # right call: it has no legitimate troubleshooting value, and the safest
    # place to stop an injection is before it can ever be retrieved.
    poisoned = [c for c in chunks if c.injection_flag]
    if poisoned:
        log.warning("%s: dropping %d chunk(s) flagged as prompt injection",
                    model_id, len(poisoned))
        execute(
            """
            INSERT INTO audit_log (actor, action, payload)
            VALUES ('ingest', 'injection_blocked', %s)
            """,
            (json.dumps({
                "model_id": model_id,
                "manual_id": manual_id,
                "count": len(poisoned),
                "patterns": sorted({p for c in poisoned for p in c.injection_hits}),
                "sections": sorted({c.section for c in poisoned}),
            }),),
        )
    chunks = [c for c in chunks if not c.injection_flag]

    # --- 5. embed --------------------------------------------------------
    vectors = embed_texts([_embedding_text(c) for c in chunks])

    # --- 6. index --------------------------------------------------------
    execute_many(
        """
        INSERT INTO doc_chunks (manual_id, model_id, section, heading,
                                page_start, page_end, content, embedding,
                                ingest_confidence, injection_flag, token_estimate)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
        """,
        [
            (manual_id, model_id, c.section, c.heading, c.page_start, c.page_end,
             c.content, _vec(v), confidence, c.token_estimate)
            for c, v in zip(chunks, vectors)
        ],
    )

    # --- 7. symbolic extraction -----------------------------------------
    codes = extract_error_codes(chunks)
    if codes:
        execute_many(
            """
            INSERT INTO error_codes (model_id, code, title, meaning, first_actions,
                                     source_manual_id, source_page)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, code) DO UPDATE
              SET title = EXCLUDED.title,
                  meaning = EXCLUDED.meaning,
                  first_actions = EXCLUDED.first_actions
            """,
            [(model_id, c.code, c.title, c.meaning, c.first_actions, manual_id, c.page)
             for c in codes],
        )

    # --- 8. coverage -----------------------------------------------------
    sections = sorted({c.section for c in chunks})
    status = _coverage_status(confidence, sections, len(chunks))
    _write_coverage(model_id, manual_id, status=status, chunk_count=len(chunks),
                    quality=confidence, sections=sections,
                    notes=_coverage_note(status, ocr_used, sections))
    _mark_manual(manual_id, confidence=confidence, ocr=ocr_used, error=None,
                 page_count=result.page_count)

    return done(status="ok" if status == "backed" else "degraded",
                chunks=len(chunks), error_codes=len(codes), confidence=confidence,
                ocr_used=ocr_used, sections=sections)


def _embedding_text(chunk) -> str:
    """Prefix the heading so the vector carries the symptom, not just the steps.

    Without this a troubleshooting chunk embeds mostly as generic imperative
    instructions ("check that", "confirm the") and matches every other symptom
    in the manual roughly equally.
    """
    return f"{chunk.heading}\n{chunk.content}" if chunk.heading else chunk.content


def _vec(values: list[float]) -> str:
    """pgvector literal."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def _coverage_status(confidence: float, sections: list[str], n_chunks: int) -> str:
    have = set(sections)
    if confidence < 0.45 or n_chunks < 5:
        return "unbacked"
    if not (CRITICAL_SECTIONS & have):
        return "unbacked"
    if confidence < 0.75 or not CRITICAL_SECTIONS.issubset(have):
        return "degraded"
    return "backed"


def _coverage_note(status: str, ocr_used: bool, sections: list[str]) -> str:
    missing = sorted(EXPECTED_SECTIONS - set(sections))
    bits = []
    if ocr_used:
        bits.append("Sourced from a scanned manual via OCR; treat quoted detail with care.")
    if missing:
        bits.append(f"Missing sections: {', '.join(missing)}.")
    if status == "unbacked":
        bits.append("Not usable for troubleshooting — escalate to a human agent.")
    elif status == "degraded":
        bits.append("Usable but incomplete — cite sources and escalate sooner.")
    return " ".join(bits) or "Complete, high-confidence manual."


def _write_coverage(model_id: str, manual_id: int | None, *, status: str,
                    chunk_count: int, quality: float, sections: list[str],
                    notes: str) -> None:
    execute(
        """
        INSERT INTO coverage_registry (model_id, status, manual_id, chunk_count,
                                       quality_score, sections_present, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (model_id) DO UPDATE
          SET status = EXCLUDED.status,
              manual_id = EXCLUDED.manual_id,
              chunk_count = EXCLUDED.chunk_count,
              quality_score = EXCLUDED.quality_score,
              sections_present = EXCLUDED.sections_present,
              notes = EXCLUDED.notes,
              updated_at = now()
        """,
        (model_id, status, manual_id, chunk_count, quality, sections, notes),
    )


def _mark_manual(manual_id: int, *, confidence: float, ocr: bool,
                 error: str | None, page_count: int | None = None) -> None:
    execute(
        """
        UPDATE manuals
           SET ingest_confidence = %s,
               ocr_applied = %s,
               ingest_error = %s,
               page_count = COALESCE(%s, page_count),
               ingested_at = now()
         WHERE id = %s
        """,
        (confidence, ocr, error, page_count, manual_id),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest FitForge service manuals")
    parser.add_argument("--model", help="ingest a single model id")
    parser.add_argument("--reingest", action="store_true",
                        help="delete and rebuild existing chunks")
    parser.add_argument("--limit", type=int, help="stop after N manuals")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sql = """
        SELECT m.id, m.model_id, m.path, m.source_type
          FROM manuals m
     LEFT JOIN coverage_registry c ON c.model_id = m.model_id
         WHERE (%(model)s::text IS NULL OR m.model_id = %(model)s)
           AND (%(reingest)s::boolean OR c.model_id IS NULL)
      ORDER BY m.model_id
    """
    manuals = query(sql, {"model": args.model, "reingest": args.reingest})
    if args.limit:
        manuals = manuals[:args.limit]

    if not manuals:
        log.info("nothing to ingest (use --reingest to rebuild)")
        return

    log.info("ingesting %d manual(s)", len(manuals))
    tally = {"ok": 0, "degraded": 0, "failed": 0, "skipped": 0}
    total_chunks = total_codes = 0

    for i, manual in enumerate(manuals, start=1):
        try:
            outcome = ingest_manual(manual, reingest=args.reingest)
        except Exception as exc:                      # noqa: BLE001
            log.exception("ingest crashed for %s", manual["model_id"])
            _mark_manual(manual["id"], confidence=0.0, ocr=False, error=str(exc)[:500])
            _write_coverage(manual["model_id"], manual["id"], status="unbacked",
                            chunk_count=0, quality=0.0, sections=[],
                            notes=f"Ingest error: {exc}")
            tally["failed"] += 1
            continue

        tally[outcome.status] += 1
        total_chunks += outcome.chunks
        total_codes += outcome.error_codes
        log.info(
            "[%d/%d] %-28s %-13s %-9s chunks=%-4d codes=%-3d conf=%.2f %s(%.1fs)",
            i, len(manuals), outcome.model_id, outcome.source_type, outcome.status,
            outcome.chunks, outcome.error_codes, outcome.confidence,
            "OCR " if outcome.ocr_used else "", outcome.duration_s,
        )

    log.info("-" * 72)
    log.info("ingest complete: %s", tally)
    log.info("chunks indexed: %d   error codes extracted: %d", total_chunks, total_codes)

    summary = query(
        "SELECT status, count(*) AS n FROM coverage_registry GROUP BY status ORDER BY status"
    )
    for row in summary:
        log.info("coverage %-9s %d models", row["status"], row["n"])


if __name__ == "__main__":
    main()
