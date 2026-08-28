"""Manual upload: validation, model detection, and the mismatch guard.

The tests here cover the ways an upload can go wrong quietly. A rejected file is
obvious to whoever uploaded it; a manual filed against the *wrong model* is not,
and it degrades retrieval for a machine that then looks well documented.
"""

from __future__ import annotations

import pytest

from services.api.app.db import query_one
from services.api.app.tools import manuals


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_non_pdf_is_rejected():
    with pytest.raises(manuals.UploadError, match="does not look like a PDF"):
        manuals.validate_pdf("notes.txt", b"just some text, not a pdf at all")


def test_empty_file_is_rejected():
    with pytest.raises(manuals.UploadError, match="empty"):
        manuals.validate_pdf("empty.pdf", b"")


def test_oversized_file_is_rejected():
    huge = b"%PDF-1.7" + b"0" * (manuals.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(manuals.UploadError, match="limit"):
        manuals.validate_pdf("huge.pdf", huge)


def test_a_real_pdf_passes_validation(sample_pdf_bytes):
    manuals.validate_pdf("manual.pdf", sample_pdf_bytes)


def test_extension_is_not_trusted():
    """A .pdf name on non-PDF bytes must still be refused."""
    with pytest.raises(manuals.UploadError):
        manuals.validate_pdf("actually_a_zip.pdf", b"PK\x03\x04 not a pdf")


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------

def restore_seeded_manual(model_id: str) -> None:
    """Put a model back on its original seeded manual.

    Several tests here deliberately replace a model's manual, and the corpus is
    shared with every other test and with the running demo. Leaving a treadmill
    filed under another treadmill's documentation would quietly corrupt
    retrieval for everything that comes after.
    """
    from pathlib import Path

    from services.api.app.config import settings
    from services.api.app.db import execute
    from services.ingest.pipeline import ingest_manual

    matches = sorted(Path(settings.manuals_dir).glob(f"{model_id}__*.pdf"))

    execute("UPDATE coverage_registry SET manual_id = NULL WHERE model_id = %s",
            (model_id,))
    execute("DELETE FROM doc_chunks WHERE model_id = %s", (model_id,))
    execute("DELETE FROM error_codes WHERE model_id = %s", (model_id,))
    execute("DELETE FROM manuals WHERE model_id = %s", (model_id,))

    if not matches:
        # The model was print-only to begin with; restore that state.
        execute(
            "INSERT INTO manuals (model_id, path, source_type, ingest_confidence) "
            "VALUES (%s, NULL, 'print_only', 0.0)", (model_id,))
        execute(
            """
            UPDATE coverage_registry
               SET status = 'unbacked', chunk_count = 0, quality_score = 0,
                   sections_present = '{}',
                   notes = 'No digital manual available for this model.'
             WHERE model_id = %s
            """,
            (model_id,))
        return

    original = matches[0]
    source_type = "scanned" if "scanned" in original.name else "born_digital"
    row = execute(
        "INSERT INTO manuals (model_id, path, source_type) VALUES (%s, %s, %s) "
        "RETURNING id",
        (model_id, str(original), source_type))
    ingest_manual({"id": row["id"], "model_id": model_id,
                   "path": str(original), "source_type": source_type},
                  reingest=True)


@pytest.fixture(scope="module", autouse=True)
def _restore_touched_models():
    """Track every model these tests overwrite, and restore it afterwards.

    Also clears the ingest_jobs rows the tests create: that table is rendered in
    the agent console, and leaving test uploads in it (complete with mismatch
    warnings) makes the real queue confusing to read.
    """
    from services.api.app.db import execute

    touched: set[str] = set()
    yield touched

    for model_id in touched:
        restore_seeded_manual(model_id)
    execute("DELETE FROM ingest_jobs WHERE uploaded_by = 'pytest'")


@pytest.fixture(scope="module")
def generated_manual(tmp_path_factory):
    """Generate the designed sample manual for an existing model."""
    from seed.generate_sample_manual import generate

    model = query_one("""
        SELECT m.id FROM models m
         WHERE m.category_id = 'treadmill' ORDER BY m.id LIMIT 1
    """)
    assert model, "seed the catalog first"
    out = tmp_path_factory.mktemp("manuals") / "sample.pdf"
    generate(model["id"], out)
    return {"model_id": model["id"], "path": out}


@pytest.fixture(scope="module")
def sample_pdf_bytes(generated_manual):
    return generated_manual["path"].read_bytes()


def test_model_is_detected_from_the_document(generated_manual):
    """The model number is printed in the manual, so we read it rather than
    asking a human to pick from 300 SKUs."""
    result = manuals.detect_model(generated_manual["path"])

    assert result.model_id == generated_manual["model_id"]
    assert result.method == "model_number"
    assert result.confidence >= 0.95


def test_detection_reports_failure_rather_than_guessing():
    """A PDF with no identifying content must return no model.

    Guessing here is the failure this whole path exists to prevent.
    """
    import io
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.pagesizes import A4

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    c.drawString(72, 720, "Shopping list: milk, bread, a new kettle.")
    c.save()

    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(buf.getvalue())
        path = Path(fh.name)

    result = manuals.detect_model(path, ocr_if_needed=False)
    assert result.model_id is None
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def test_upload_creates_a_queued_job(sample_pdf_bytes):
    """A queued job is created but deliberately not processed here."""
    job = manuals.create_job(filename="sample.pdf", content=sample_pdf_bytes,
                             model_id=None, uploaded_by="pytest")

    assert job["job_id"]
    stored = manuals.get_job(job["job_id"])
    assert stored["status"] == "queued"
    assert stored["model_id"] is None       # detection has not run yet


def test_upload_rejects_an_unknown_model(sample_pdf_bytes):
    with pytest.raises(manuals.UploadError, match="No such model"):
        manuals.create_job(filename="sample.pdf", content=sample_pdf_bytes,
                           model_id="FF-XX-DOES-NOT-EXIST-000")


def test_full_ingestion_makes_the_model_searchable(generated_manual,
                                                   sample_pdf_bytes,
                                                   _restore_touched_models):
    """End to end: upload -> detect -> ingest -> retrievable.

    This is the whole point of the feature. A model with no documentation must
    become one the agent can actually troubleshoot.
    """
    from services.api.app.tools import knowledge

    model_id = generated_manual["model_id"]
    _restore_touched_models.add(model_id)

    job = manuals.create_job(filename="sample.pdf", content=sample_pdf_bytes,
                             model_id=None, uploaded_by="pytest")
    manuals.process_job(job["job_id"])

    done = manuals.get_job(job["job_id"])
    assert done["status"] == "done", done.get("error")
    assert done["model_id"] == model_id
    assert done["result"]["chunks"] > 5
    assert done["result"]["error_codes"] > 0

    coverage = knowledge.get_coverage(model_id)
    assert coverage["status"] in ("backed", "degraded")

    # And the content is genuinely retrievable, scoped to this model.
    found = knowledge.search_manual(
        model_id=model_id, query_text="the running belt keeps slipping",
    )
    assert found.chunks
    for chunk in found.chunks:
        owner = query_one("SELECT model_id FROM doc_chunks WHERE id = %s",
                          (chunk.chunk_id,))
        assert owner["model_id"] == model_id


def test_mismatch_between_document_and_chosen_model_is_flagged(
        sample_pdf_bytes, generated_manual, _restore_touched_models):
    """Filing a manual under the wrong SKU is allowed but never silent.

    The uploader may have a legitimate reason, so their choice wins — but a
    mis-filed manual poisons retrieval for a model that then looks documented,
    so the disagreement is recorded for review.
    """
    wrong = query_one(
        "SELECT id FROM models WHERE id <> %s AND category_id = 'treadmill' LIMIT 1",
        (generated_manual["model_id"],),
    )

    _restore_touched_models.add(wrong["id"])

    job = manuals.create_job(filename="sample.pdf", content=sample_pdf_bytes,
                             model_id=wrong["id"], uploaded_by="pytest")
    manuals.process_job(job["job_id"])

    done = manuals.get_job(job["job_id"])
    detection = done["detection"]

    assert detection["detected_model_id"] == generated_manual["model_id"]
    assert "mismatch" in detection
    assert generated_manual["model_id"] in detection["mismatch"]
    # The uploader's choice still won.
    assert done["model_id"] == wrong["id"]


# ---------------------------------------------------------------------------
# Backfill queue
# ---------------------------------------------------------------------------

def test_backfill_queue_lists_only_gaps():
    for entry in manuals.backfill_queue():
        assert entry["status"] in ("unbacked", "degraded")


def test_backfill_queue_is_ordered_by_traffic():
    """"17 models are unbacked" is not actionable; "these three account for most
    of your sessions" is."""
    queue = manuals.backfill_queue()
    sessions = [m["sessions"] for m in queue]
    assert sessions == sorted(sessions, reverse=True)


def test_model_search_finds_by_id_and_name():
    model = query_one("SELECT id, name FROM models LIMIT 1")

    by_id = manuals.search_models(model["id"])
    assert any(m["id"] == model["id"] for m in by_id)

    by_name = manuals.search_models(model["name"].split()[0])
    assert by_name
