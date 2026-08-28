-- ---------------------------------------------------------------------------
-- Uploaded manuals and their ingestion jobs.
--
-- Ingestion is not fast — a scanned manual costs ~7s of OCR before anything can
-- be indexed — so an upload cannot be a synchronous request. The upload returns
-- a job, the work happens in the background, and the console polls. This table
-- is what makes that observable rather than a spinner that might mean anything.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Null until the model is resolved: a file can be uploaded before we know
    -- which SKU it documents, and detection may need the customer to confirm.
    model_id      TEXT REFERENCES models(id),
    manual_id     BIGINT REFERENCES manuals(id) ON DELETE SET NULL,
    filename      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL DEFAULT 0,
    -- sha256 of the file. Re-uploading the same bytes for the same model is a
    -- no-op rather than a duplicate index.
    content_hash  TEXT,

    status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                    ('queued', 'detecting', 'awaiting_model', 'ingesting',
                     'done', 'failed')),
    -- Human-readable stage, surfaced live in the console.
    stage         TEXT,
    uploaded_by   TEXT,

    -- How the model was decided, so a wrong auto-detection is traceable.
    detection     JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Populated on completion: chunks, error codes, confidence, OCR used.
    result        JSONB NOT NULL DEFAULT '{}'::jsonb,
    error         TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ingest_jobs_status_idx  ON ingest_jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ingest_jobs_model_idx   ON ingest_jobs (model_id);
CREATE INDEX IF NOT EXISTS ingest_jobs_hash_idx    ON ingest_jobs (content_hash);

-- Distinguishes a manual that arrived through the console from one produced by
-- the seed generator, which matters when reading the coverage report.
ALTER TABLE manuals ADD COLUMN IF NOT EXISTS uploaded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE manuals ADD COLUMN IF NOT EXISTS original_filename TEXT;
