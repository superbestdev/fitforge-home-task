-- ---------------------------------------------------------------------------
-- FitForge agentic support — core schema
--
-- Design note: one Postgres holds the catalog, the vector index, the session
-- state and the LangGraph checkpoints. That is deliberate. The corpus is a few
-- hundred SKUs (low hundreds of thousands of chunks), which pgvector handles
-- comfortably, and keeping the catalog transactionally consistent with the
-- retrieval index removes a whole class of "the agent quoted a part that no
-- longer exists" bugs. See docs/04-tradeoffs.md.
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ===========================================================================
-- PRODUCT CATALOG
-- ===========================================================================

CREATE TABLE product_categories (
    id          TEXT PRIMARY KEY,          -- 'treadmill', 'bike', 'rower', ...
    name        TEXT NOT NULL,
    -- Safety class drives what we will and will not talk a customer through.
    -- 'high_voltage' categories refuse DIY internal repair outright.
    safety_class TEXT NOT NULL DEFAULT 'standard'
                 CHECK (safety_class IN ('standard', 'high_voltage', 'high_tension'))
);

CREATE TABLE product_families (
    id          TEXT PRIMARY KEY,          -- 'pacer', 'summit', 'velodrome', ...
    category_id TEXT NOT NULL REFERENCES product_categories(id),
    name        TEXT NOT NULL
);

CREATE TABLE models (
    id            TEXT PRIMARY KEY,        -- SKU, e.g. 'FF-TM-PACER-350'
    family_id     TEXT NOT NULL REFERENCES product_families(id),
    category_id   TEXT NOT NULL REFERENCES product_categories(id),
    name          TEXT NOT NULL,           -- 'Pacer 350 Treadmill'
    model_year    INT  NOT NULL,
    -- Serial numbers encode the model. Cheapest, most reliable identification
    -- path we have, so it gets a first-class column and an index.
    serial_prefix TEXT NOT NULL,
    msrp_cents    INT  NOT NULL,
    -- Distinguishing features used by the guided-narrowing identification path
    -- when the customer has no order history and no serial ("what colour is the
    -- console bezel?"). Shape: {"console":"7in colour","deck":"black","fold":true}
    features      JSONB NOT NULL DEFAULT '{}'::jsonb,
    released_on   DATE NOT NULL,
    discontinued  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX models_serial_prefix_idx ON models (serial_prefix);
CREATE INDEX models_category_idx      ON models (category_id);
CREATE INDEX models_name_trgm_idx     ON models USING gin (name gin_trgm_ops);

-- Warranty terms are per model and are read by the deterministic policy engine.
-- The LLM never sees these rows; it only ever sees the engine's verdict.
CREATE TABLE warranty_terms (
    model_id            TEXT PRIMARY KEY REFERENCES models(id),
    frame_months        INT NOT NULL,
    parts_months        INT NOT NULL,
    electronics_months  INT NOT NULL,
    labor_months        INT NOT NULL,
    consumables_covered BOOLEAN NOT NULL DEFAULT FALSE,
    -- Some categories void coverage for commercial use; recorded so the engine
    -- can explain *why* something was denied.
    notes               TEXT
);

CREATE TABLE parts (
    part_number   TEXT PRIMARY KEY,        -- 'FF-TM-PACER-350-BELT'
    model_id      TEXT NOT NULL REFERENCES models(id),
    name          TEXT NOT NULL,
    -- Category maps onto the warranty terms above.
    part_class    TEXT NOT NULL CHECK (part_class IN
                    ('frame', 'mechanical', 'electronics', 'consumable')),
    price_cents   INT NOT NULL,
    -- Symptom tags let us go fault -> part deterministically instead of asking
    -- the model to invent a part number.
    symptom_tags  TEXT[] NOT NULL DEFAULT '{}',
    customer_replaceable BOOLEAN NOT NULL DEFAULT TRUE,
    -- 'restricted' parts are never offered for self-service replacement.
    safety_class  TEXT NOT NULL DEFAULT 'standard'
                  CHECK (safety_class IN ('standard', 'restricted')),
    in_stock      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX parts_model_idx    ON parts (model_id);
CREATE INDEX parts_symptom_idx  ON parts USING gin (symptom_tags);

-- Error codes are extracted from manuals into a real table at ingest time.
-- Looking up "E7" should be an indexed read, not a cosine similarity search.
CREATE TABLE error_codes (
    id            BIGSERIAL PRIMARY KEY,
    model_id      TEXT NOT NULL REFERENCES models(id),
    code          TEXT NOT NULL,
    title         TEXT NOT NULL,
    meaning       TEXT NOT NULL,
    first_actions TEXT NOT NULL,
    likely_parts  TEXT[] NOT NULL DEFAULT '{}',
    source_manual_id BIGINT,
    source_page   INT,
    UNIQUE (model_id, code)
);

CREATE INDEX error_codes_lookup_idx ON error_codes (model_id, code);

-- ===========================================================================
-- CUSTOMERS & ORDERS
-- ===========================================================================

CREATE TABLE customers (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email      TEXT UNIQUE NOT NULL,
    phone      TEXT,
    full_name  TEXT NOT NULL,
    -- Shipping address for replacement parts.
    address    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX customers_phone_idx ON customers (phone);

-- Equipment purchases. This is identification path #1 and by far the best one.
CREATE TABLE orders (
    id            TEXT PRIMARY KEY,        -- 'FF-2024-0001234'
    customer_id   UUID NOT NULL REFERENCES customers(id),
    model_id      TEXT NOT NULL REFERENCES models(id),
    serial_number TEXT UNIQUE NOT NULL,
    purchased_at  DATE NOT NULL,
    channel       TEXT NOT NULL DEFAULT 'web',
    -- Commercial use can void parts of the warranty; the policy engine reads it.
    commercial_use BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_serial_idx   ON orders (serial_number);

-- ===========================================================================
-- KNOWLEDGE LAYER
-- ===========================================================================

CREATE TABLE manuals (
    id            BIGSERIAL PRIMARY KEY,
    model_id      TEXT NOT NULL REFERENCES models(id),
    path          TEXT,
    -- How the source arrived. 'print_only' means it physically exists but we
    -- have no digital copy — the agent must know it is blind, not guess.
    source_type   TEXT NOT NULL CHECK (source_type IN
                    ('born_digital', 'scanned', 'print_only', 'missing')),
    page_count    INT NOT NULL DEFAULT 0,
    ocr_applied   BOOLEAN NOT NULL DEFAULT FALSE,
    -- 0.0-1.0. Scanned/OCR'd sources are penalised; low confidence lowers the
    -- escalation threshold for every session touching this model.
    ingest_confidence REAL NOT NULL DEFAULT 1.0,
    ingest_error  TEXT,
    ingested_at   TIMESTAMPTZ
);

CREATE INDEX manuals_model_idx ON manuals (model_id);

CREATE TABLE doc_chunks (
    id            BIGSERIAL PRIMARY KEY,
    manual_id     BIGINT NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
    -- Denormalised on purpose: EVERY retrieval query filters on model_id, and
    -- cross-model contamination (a rower fix offered for a treadmill) is the
    -- most damaging retrieval failure in this domain.
    model_id      TEXT NOT NULL REFERENCES models(id),
    section       TEXT NOT NULL,           -- 'troubleshooting', 'parts', ...
    heading       TEXT,
    page_start    INT,
    page_end      INT,
    content       TEXT NOT NULL,
    -- Postgres full-text vector; the BM25-ish half of hybrid retrieval.
    tsv           TSVECTOR,
    embedding     VECTOR(768),
    ingest_confidence REAL NOT NULL DEFAULT 1.0,
    -- Set at ingest if the chunk text looks like it is trying to give the model
    -- instructions. Supplier PDFs are an untrusted input channel.
    injection_flag BOOLEAN NOT NULL DEFAULT FALSE,
    token_estimate INT NOT NULL DEFAULT 0
);

CREATE INDEX doc_chunks_model_idx    ON doc_chunks (model_id);
CREATE INDEX doc_chunks_section_idx  ON doc_chunks (model_id, section);
CREATE INDEX doc_chunks_tsv_idx      ON doc_chunks USING gin (tsv);
-- IVFFlat needs data before it can be built well; the ingest job creates the
-- ANN index after load. A plain index here keeps small-corpus queries exact.
CREATE INDEX doc_chunks_embedding_idx ON doc_chunks
    USING hnsw (embedding vector_cosine_ops);

CREATE FUNCTION doc_chunks_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english',
        coalesce(NEW.heading, '') || ' ' || coalesce(NEW.content, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER doc_chunks_tsv_trg
    BEFORE INSERT OR UPDATE OF content, heading ON doc_chunks
    FOR EACH ROW EXECUTE FUNCTION doc_chunks_tsv_update();

-- The agent asks this table "do I actually have documentation for this model?"
-- before it starts troubleshooting. Knowing what you don't know is the whole
-- cold-start answer. See docs/08-cold-start.md.
CREATE TABLE coverage_registry (
    model_id      TEXT PRIMARY KEY REFERENCES models(id),
    status        TEXT NOT NULL CHECK (status IN ('backed', 'degraded', 'unbacked')),
    manual_id     BIGINT REFERENCES manuals(id),
    chunk_count   INT NOT NULL DEFAULT 0,
    quality_score REAL NOT NULL DEFAULT 0.0,
    sections_present TEXT[] NOT NULL DEFAULT '{}',
    notes         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- SESSIONS & ISSUE THREADS
-- ===========================================================================

CREATE TABLE sessions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID REFERENCES customers(id),
    channel     TEXT NOT NULL DEFAULT 'web_chat',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'escalated', 'closed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);

-- A session holds N independent issue threads. This table is the reason the
-- multi-issue requirement is tractable: state is per-issue, not per-session,
-- so suspending thread A to handle thread B loses nothing.
CREATE TABLE issue_threads (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INT NOT NULL,          -- 1, 2, 3... order raised in session
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                      ('new', 'diagnosing', 'awaiting_customer', 'awaiting_part',
                       'resolved', 'unresolvable', 'escalated')),
    -- Threads may concern DIFFERENT products in the same session.
    model_id        TEXT REFERENCES models(id),
    symptom_summary TEXT NOT NULL DEFAULT '',
    steps           JSONB NOT NULL DEFAULT '[]'::jsonb,
    ruled_out       JSONB NOT NULL DEFAULT '[]'::jsonb,
    citations       JSONB NOT NULL DEFAULT '[]'::jsonb,
    candidate_part  TEXT REFERENCES parts(part_number),
    quote_id        UUID,
    step_budget_used INT NOT NULL DEFAULT 0,
    tool_failures   INT NOT NULL DEFAULT 0,
    resolution_note TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE INDEX issue_threads_session_idx ON issue_threads (session_id);
CREATE INDEX issue_threads_status_idx  ON issue_threads (status);

CREATE TABLE session_messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    -- Every message is attributed to an issue thread where possible. This is
    -- what lets the handoff packet reconstruct "what was said about issue 2".
    issue_id   UUID REFERENCES issue_threads(id) ON DELETE SET NULL,
    role       TEXT NOT NULL CHECK (role IN ('customer', 'agent', 'system', 'human_agent')),
    content    TEXT NOT NULL,
    meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX session_messages_session_idx ON session_messages (session_id, id);

-- Models identified during a session, with the evidence that identified them.
CREATE TABLE verified_models (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model_id   TEXT NOT NULL REFERENCES models(id),
    order_id   TEXT REFERENCES orders(id),
    -- 'order_lookup' | 'serial_number' | 'guided_narrowing' | 'customer_confirmed'
    method     TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, model_id)
);

-- ===========================================================================
-- COMMERCE  (deterministic paths only — no LLM output reaches these tables)
-- ===========================================================================

CREATE TABLE quotes (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id        UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    issue_id          UUID REFERENCES issue_threads(id) ON DELETE SET NULL,
    part_number       TEXT NOT NULL REFERENCES parts(part_number),
    quantity          INT NOT NULL DEFAULT 1,
    unit_price_cents  INT NOT NULL,
    shipping_cents    INT NOT NULL DEFAULT 0,
    tax_cents         INT NOT NULL DEFAULT 0,
    total_cents       INT NOT NULL,
    -- Full output of the warranty policy engine, kept for audit and for
    -- explaining the decision back to the customer verbatim.
    coverage_decision JSONB NOT NULL,
    covered           BOOLEAN NOT NULL,
    -- SHA-256 over the exact figures shown to the customer. place_order refuses
    -- to run unless the confirmation it receives hashes to this value, so the
    -- model cannot quietly change the price between quote and charge.
    confirmation_hash TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending', 'confirmed', 'paid', 'ordered', 'expired', 'cancelled')),
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX quotes_session_idx ON quotes (session_id);

CREATE TABLE payments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    quote_id        UUID NOT NULL REFERENCES quotes(id),
    -- Replay protection: the same key never charges twice, however many times
    -- an over-eager agent loop retries the tool call.
    idempotency_key TEXT UNIQUE NOT NULL,
    amount_cents    INT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('authorized', 'captured', 'declined', 'failed')),
    psp_reference   TEXT,
    -- We store the last four only. Card data never enters our services, and
    -- never enters the model's context.
    card_last4      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE part_orders (
    id           TEXT PRIMARY KEY,
    session_id   UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    issue_id     UUID REFERENCES issue_threads(id) ON DELETE SET NULL,
    customer_id  UUID NOT NULL REFERENCES customers(id),
    quote_id     UUID NOT NULL REFERENCES quotes(id),
    payment_id   UUID REFERENCES payments(id),
    part_number  TEXT NOT NULL REFERENCES parts(part_number),
    quantity     INT NOT NULL DEFAULT 1,
    ship_to      JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'placed'
                 CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled')),
    eta_days     INT NOT NULL DEFAULT 5,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- HANDOFF & AUDIT
-- ===========================================================================

CREATE TABLE handoffs (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    -- Which escalation trigger fired. Tracking this by reason is the single
    -- most useful production signal we collect. See docs/06-observability.md.
    reason      TEXT NOT NULL,
    detail      TEXT,
    -- The complete HandoffPacket: customer, verified models, every issue thread
    -- with its diagnostic history, citations, quotes, orders, and a summary.
    packet      JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'claimed', 'resolved')),
    claimed_by  TEXT,
    claimed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX handoffs_status_idx ON handoffs (status, created_at);

-- Append-only record of every consequential action: coverage verdicts, charges,
-- orders, escalations. This is what you hand to an auditor when a customer
-- disputes a charge the agent took.
CREATE TABLE audit_log (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    issue_id   UUID,
    actor      TEXT NOT NULL,              -- 'policy_engine', 'agent', 'customer', 'system'
    action     TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_session_idx ON audit_log (session_id, id);

-- Per-call LLM accounting. Drives the cost model in docs/09-cost-model.md and
-- the latency panels; also how we prove which node is burning the budget.
CREATE TABLE llm_calls (
    id                BIGSERIAL PRIMARY KEY,
    session_id        UUID,
    issue_id          UUID,
    node              TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    latency_ms        INT NOT NULL DEFAULT 0,
    ok                BOOLEAN NOT NULL DEFAULT TRUE,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX llm_calls_session_idx ON llm_calls (session_id);
CREATE INDEX llm_calls_node_idx    ON llm_calls (node, created_at);
