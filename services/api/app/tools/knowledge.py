"""Knowledge tools: manual retrieval, error-code lookup, coverage checks.

Retrieval is hybrid — dense vectors plus Postgres full-text — fused with
Reciprocal Rank Fusion. Two decisions are worth calling out:

**Every query is filtered by model_id, without exception.** Cross-model
contamination is the most damaging retrieval failure in this domain: confidently
telling someone with a rower to adjust their rear roller bolts is worse than
saying nothing at all. The filter lives in the SQL, not in a prompt, so it
cannot be reasoned away.

**No cross-encoder reranker.** It would be the slowest component in the stack on
CPU, and RRF over a candidate set already narrowed to a single model recovers
most of the benefit. That is a real trade-off — see docs/04-tradeoffs.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import settings
from ..db import query, query_one

log = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: int
    section: str
    heading: str | None
    content: str
    page_start: int | None
    page_end: int | None
    ingest_confidence: float
    rrf_score: float
    # Cosine similarity, kept separate from the fusion score because it is the
    # interpretable one. The escalation threshold compares against this, not
    # against RRF (whose values sit around 1/60 and mean nothing on their own).
    vector_score: float | None
    text_score: float | None

    def citation(self) -> dict:
        if self.page_start is None:
            pages = None
        elif self.page_start == self.page_end:
            pages = "p." + str(self.page_start)
        else:
            pages = "pp." + str(self.page_start) + "-" + str(self.page_end)
        return {
            "chunk_id": self.chunk_id,
            "section": self.section,
            "heading": self.heading,
            "pages": pages,
            "confidence": round(self.ingest_confidence, 2),
        }


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    best_vector_score: float = 0.0
    coverage_status: str | None = None
    coverage_note: str | None = None

    @property
    def is_confident(self) -> bool:
        return self.best_vector_score >= settings.retrieval_min_score

    def as_context(self, max_chars: int = 4000) -> str:
        """Render retrieved chunks for a prompt.

        The consuming prompt wraps this in explicit delimiters and states that
        everything inside is reference data, never instructions. That is the
        second half of the injection defence; the first is that flagged chunks
        are never indexed at all.
        """
        parts: list[str] = []
        total = 0
        for i, c in enumerate(self.chunks, start=1):
            header = "[" + str(i) + "] section=" + c.section
            if c.heading:
                header += " | " + c.heading
            if c.page_start:
                header += " | pages " + str(c.page_start) + "-" + str(c.page_end)
            block = header + "\n" + c.content
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n\n---\n\n".join(parts)


HYBRID_SQL = """
WITH vec AS (
    SELECT id,
           row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank,
           1 - (embedding <=> %(qvec)s::vector) AS score
      FROM doc_chunks
     WHERE model_id = %(model_id)s
       AND embedding IS NOT NULL
       AND (%(section)s::text IS NULL OR section = %(section)s)
     ORDER BY embedding <=> %(qvec)s::vector
     LIMIT %(candidates)s
),
fts AS (
    SELECT d.id,
           row_number() OVER (ORDER BY ts_rank_cd(d.tsv, q.query) DESC) AS rank,
           ts_rank_cd(d.tsv, q.query) AS score
      FROM doc_chunks d,
           plainto_tsquery('english', %(qtext)s) AS q(query)
     WHERE d.model_id = %(model_id)s
       AND d.tsv @@ q.query
       AND (%(section)s::text IS NULL OR d.section = %(section)s)
     ORDER BY ts_rank_cd(d.tsv, q.query) DESC
     LIMIT %(candidates)s
)
SELECT c.id AS chunk_id, c.section, c.heading, c.content,
       c.page_start, c.page_end, c.ingest_confidence,
       COALESCE(1.0 / (%(rrf_k)s + vec.rank), 0.0)
     + COALESCE(1.0 / (%(rrf_k)s + fts.rank), 0.0) AS rrf_score,
       vec.score AS vector_score,
       fts.score AS text_score
  FROM doc_chunks c
  LEFT JOIN vec ON vec.id = c.id
  LEFT JOIN fts ON fts.id = c.id
 WHERE vec.id IS NOT NULL OR fts.id IS NOT NULL
 ORDER BY rrf_score DESC
 LIMIT %(top_k)s
"""

TEXT_ONLY_SQL = """
SELECT d.id AS chunk_id, d.section, d.heading, d.content,
       d.page_start, d.page_end, d.ingest_confidence,
       ts_rank_cd(d.tsv, q.query) AS rrf_score,
       NULL::float AS vector_score,
       ts_rank_cd(d.tsv, q.query) AS text_score
  FROM doc_chunks d, plainto_tsquery('english', %(qtext)s) AS q(query)
 WHERE d.model_id = %(model_id)s
   AND d.tsv @@ q.query
   AND (%(section)s::text IS NULL OR d.section = %(section)s)
 ORDER BY rrf_score DESC
 LIMIT %(top_k)s
"""


def search_manual(
    *,
    model_id: str,
    query_text: str,
    section: str | None = None,
    top_k: int | None = None,
) -> RetrievalResult:
    """Search one model's manual. Returns chunks plus a confidence signal."""
    from services.ingest.embed import embed_query

    coverage = get_coverage(model_id)
    result = RetrievalResult(
        coverage_status=coverage.get("status") if coverage else "unbacked",
        coverage_note=(coverage.get("notes") if coverage
                       else "No coverage record exists for this model."),
    )

    # No documentation means no search. Returning empty here, rather than
    # running a query against zero rows, keeps the reason legible to the caller
    # — and the escalation check reads exactly this.
    if result.coverage_status == "unbacked":
        return result

    limit = top_k or settings.retrieval_top_k

    try:
        qvec = embed_query(query_text)
    except Exception as exc:                            # noqa: BLE001
        # Degrade to full-text rather than failing the turn. A keyword match is
        # far more useful to the customer than an apology.
        log.warning("embedding failed; falling back to full-text only: %s", exc)
        return _text_only_search(model_id, query_text, section, limit, result)

    rows = query(HYBRID_SQL, {
        "qvec": _vec_literal(qvec),
        "qtext": query_text,
        "model_id": model_id,
        "section": section,
        "candidates": settings.retrieval_candidates,
        "rrf_k": settings.rrf_k,
        "top_k": limit,
    })

    result.chunks = [RetrievedChunk(**row) for row in rows]
    result.best_vector_score = max(
        (c.vector_score or 0.0 for c in result.chunks), default=0.0
    )
    return result


def _text_only_search(model_id: str, query_text: str, section: str | None,
                      top_k: int, result: RetrievalResult) -> RetrievalResult:
    rows = query(TEXT_ONLY_SQL, {
        "qtext": query_text, "model_id": model_id,
        "section": section, "top_k": top_k,
    })
    result.chunks = [RetrievedChunk(**row) for row in rows]
    # Without vectors there is no comparable confidence number. Report the
    # threshold itself so a degraded search does not on its own trip the
    # low-confidence escalation — the tool-failure counter covers that case.
    result.best_vector_score = settings.retrieval_min_score if rows else 0.0
    return result


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# ---------------------------------------------------------------------------
# Symbolic lookups
# ---------------------------------------------------------------------------

def lookup_error_code(*, model_id: str, code: str) -> dict | None:
    """Exact lookup of a console error code.

    Error codes are extracted into a real table at ingest time precisely so this
    is a keyed read rather than a similarity search. "E7" and "E1" are nearly
    identical strings that embed almost identically — a vector index would
    cheerfully confuse them, and the two have completely different causes.
    """
    normalised = code.strip().upper().replace(" ", "").replace("-", "")
    row = query_one(
        """
        SELECT code, title, meaning, first_actions, likely_parts, source_page
          FROM error_codes WHERE model_id = %s AND code = %s
        """,
        (model_id, normalised),
    )
    if row:
        return dict(row)

    # Console fonts make O/0 and I/1 ambiguous, and customers transcribe them
    # wrong constantly. Try the obvious confusions before giving up.
    for variant in _code_variants(normalised):
        row = query_one(
            """
            SELECT code, title, meaning, first_actions, likely_parts, source_page
              FROM error_codes WHERE model_id = %s AND code = %s
            """,
            (model_id, variant),
        )
        if row:
            out = dict(row)
            out["note"] = f"Interpreted '{code}' as '{variant}'."
            return out
    return None


def _code_variants(code: str) -> list[str]:
    swaps = {"O": "0", "0": "O", "I": "1", "1": "I", "S": "5", "5": "S"}
    out = []
    for i, ch in enumerate(code):
        if ch in swaps:
            out.append(code[:i] + swaps[ch] + code[i + 1:])
    return out


def get_coverage(model_id: str) -> dict | None:
    """What documentation do we actually have for this model?"""
    row = query_one(
        """
        SELECT c.model_id, c.status, c.chunk_count, c.quality_score,
               c.sections_present, c.notes, m.source_type, m.ocr_applied
          FROM coverage_registry c
          LEFT JOIN manuals m ON m.id = c.manual_id
         WHERE c.model_id = %s
        """,
        (model_id,),
    )
    return dict(row) if row else None


def list_error_codes(model_id: str) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT code, title FROM error_codes WHERE model_id = %s ORDER BY code",
        (model_id,),
    )]
