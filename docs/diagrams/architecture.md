# Diagrams

Mermaid source. GitHub renders these inline.

## System architecture

```mermaid
flowchart TB
    subgraph clients["Clients"]
        chat["Chat widget<br/>:5173/"]
        console["Agent console<br/>:5173/console"]
    end

    gw["FastAPI gateway<br/>WebSocket + REST"]

    subgraph orch["Session orchestrator — LangGraph"]
        pre["precheck<br/><i>safety · human request · identifiers</i>"]
        rt["route<br/><i>fast paths, then 1.5B classifier</i>"]
        gate{{"THE GATE<br/>model verified?"}}
        idn["identify<br/><i>order → serial → narrowing</i>"]
        oi["open_issue"]
        dg["diagnose<br/><i>one step, 3B model</i>"]
        sw["switch_issue"]
        sp["select_part"]
        qp["quote_part"]
        cf["confirm"]
        ho["handoff<br/><i>builds the packet</i>"]
    end

    subgraph det["Deterministic layer — no LLM output enters here"]
        pol["Policy engine<br/>warranty · safety · escalation"]
        tools["Tool registry<br/>typed, pure"]
    end

    subgraph know["Knowledge layer"]
        ret["Hybrid retrieval<br/>pgvector + FTS, fused by RRF<br/><b>always scoped by model_id</b>"]
        sym["Symbolic facts<br/>error_codes"]
        cov["Coverage registry<br/>backed · degraded · unbacked"]
    end

    db[("Postgres 17<br/>pgvector · tsvector<br/>catalog · threads · audit<br/>checkpoints")]
    llm["Ollama<br/>qwen2.5 3B + 1.5B<br/>nomic-embed-text"]
    psp["Mock PSP<br/>tokenize · charge"]

    chat <-->|WS| gw
    console <-->|WS + REST| gw
    gw --> pre --> rt --> gate
    gate -->|no| idn
    gate -->|yes| oi & dg & sw & sp
    idn --> oi
    oi --> dg
    dg -->|needs a part| sp --> qp --> cf
    dg -->|any trigger| ho
    qp --> ho

    dg --> ret
    dg --> sym
    ret --> cov
    sp --> tools
    qp --> pol
    cf --> psp
    ho --> db

    tools --> db
    pol --> db
    ret --> db
    dg -.-> llm
    rt -.-> llm
    ret -.->|embeddings| llm

    console -.->|escalation queue via Redis| ho

    classDef deterministic fill:#0f3d2e,stroke:#22c55e,color:#e6edf5
    classDef probabilistic fill:#1e3a5f,stroke:#3b82f6,color:#e6edf5
    classDef store fill:#2a1f3d,stroke:#a78bfa,color:#e6edf5
    class det,pol,tools,psp deterministic
    class dg,rt,llm probabilistic
    class db,know,ret,sym,cov store
```

Green is deterministic, blue involves a model, purple is storage. The commerce
path is entirely green — that is the point.

## Ingestion pipeline

```mermaid
flowchart LR
    pdf["Service manual PDF"] --> cls{"Text density<br/>per page"}
    cls -->|born-digital| ext["Extract text<br/>pypdfium2"]
    cls -->|">30% image-only"| ocr["OCR<br/>ocrmypdf + Tesseract<br/><i>deskew · clean · rotate</i>"]
    ocr --> ext
    cls -->|no file| gap["print_only"]

    ext --> qual["Text-quality score<br/><i>×0.8 if OCR was used</i>"]
    qual --> chunk["Structure-aware chunking<br/><b>one chunk per symptom</b>"]

    chunk --> inj{"Injection<br/>screen"}
    inj -->|flagged| drop["DROPPED<br/>+ audit_log"]
    inj -->|clean| emb["Embed<br/>nomic-embed-text"]
    chunk --> sym["Symbolic extraction<br/>error codes → table"]

    emb --> idx[("doc_chunks<br/>pgvector + tsvector")]
    sym --> ec[("error_codes")]
    qual --> reg[("coverage_registry<br/>backed | degraded | unbacked")]
    gap --> reg

    classDef bad fill:#3d1f1f,stroke:#ef4444,color:#e6edf5
    classDef store fill:#2a1f3d,stroke:#a78bfa,color:#e6edf5
    class drop,gap bad
    class idx,ec,reg store
```

## The multi-issue state model

```mermaid
erDiagram
    SESSION ||--o{ ISSUE_THREAD : "has many"
    SESSION ||--o{ VERIFIED_MODEL : "identified"
    SESSION ||--o{ SESSION_MESSAGE : "transcript"
    SESSION ||--o{ HANDOFF : "escalates to"
    ISSUE_THREAD }o--|| MODEL : "is about"
    ISSUE_THREAD ||--o{ DIAGNOSTIC_STEP : "records"
    ISSUE_THREAD }o--o| QUOTE : "may produce"
    QUOTE ||--o| PAYMENT : "may be paid by"
    QUOTE ||--o| PART_ORDER : "becomes"
    MODEL ||--o{ PART : "has"
    MODEL ||--|| WARRANTY_TERMS : "governed by"
    MODEL ||--|| COVERAGE_REGISTRY : "documented by"

    ISSUE_THREAD {
        int seq
        string title
        string status "independent per thread"
        string model_id "may differ per thread"
        json steps
        json ruled_out
        int step_budget_used
    }
```

The critical detail: `model_id` and `status` live on the **thread**, not the
session. That is what lets one session hold a resolved treadmill issue and an
escalated bike issue at the same time.

## A diagnostic turn

```mermaid
sequenceDiagram
    participant C as Customer
    participant G as Graph
    participant P as Policy (Python)
    participant R as Retrieval (SQL)
    participant M as Model (3B)
    participant D as Postgres

    C->>G: "the belt keeps slipping"
    G->>P: safety screen
    P-->>G: ok (~0 ms, no LLM)
    G->>G: route — fast path or 1.5B
    G->>G: GATE — model verified?
    G->>R: search_manual(model_id, symptom)
    R->>D: pgvector + FTS, fused by RRF
    D-->>R: 6 chunks, scoped to this model
    R-->>G: chunks + confidence
    alt confidence below threshold
        G->>D: escalate(low_retrieval_confidence)
        G-->>C: "I don't want to send you down the wrong path"
    else confident
        G->>M: one step, schema-constrained
        M-->>G: {message, status, ruled_out}
        G->>G: guards — no premature part, no repeat
        G->>D: append step to issue_thread
        G-->>C: single next step + citations
    end
```
