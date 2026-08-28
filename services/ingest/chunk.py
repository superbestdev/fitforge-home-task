"""Structure-aware chunking, symbolic extraction, and injection screening.

Three ideas drive this module:

1. **Chunk on document structure, not on character count.** A service manual has
   real sections, and a troubleshooting entry is a self-contained unit. Splitting
   every 1000 characters would routinely cut a diagnostic procedure in half and
   hand the agent step 3 without steps 1 and 2.

2. **Extract facts into tables, not just text into an index.** An error code is
   a key. Looking up "E7" should be an indexed read that is always right, not a
   cosine similarity search that is usually right.

3. **Treat manual text as untrusted input.** These PDFs come from suppliers. A
   PDF that contains "ignore all previous instructions" is an attack delivered
   through the knowledge base, and it has to be caught at ingest, before the
   text ever reaches a prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TARGET_CHARS = 950
MAX_CHARS = 1500
OVERLAP_CHARS = 120

# Tolerant of OCR damage: the dash between number and title may be mangled, and
# a stray character may sit inside the heading.
SECTION_RE = re.compile(
    r"^\s*SECT[I1L]ON\s+(\d+)\s*[-–—:.]*\s*([A-Z][A-Z0-9 /&'\-]{3,40})\s*$",
    re.MULTILINE,
)

SECTION_ALIASES = {
    "MODEL IDENTIFICATION": "identification",
    "SAFETY": "safety",
    "MAINTENANCE SCHEDULE": "maintenance",
    "TROUBLESHOOTING": "troubleshooting",
    "ERROR CODES": "error_codes",
    "PARTS LIST": "parts",
    "WARRANTY": "warranty",
    "SUPPLIER BULLETIN": "bulletin",
}

SYMPTOM_RE = re.compile(r"^\s*Symptom:\s*(.+)$", re.MULTILINE)

# An error code line: a letter followed by 1-2 digits, at the start of a line.
ERROR_CODE_RE = re.compile(r"^\s*([A-Z]{1,2}\s?\d{1,2})\b[\s.:-]*(.*)$")

# ---------------------------------------------------------------------------
# Prompt-injection screening
# ---------------------------------------------------------------------------

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
        r"disregard\s+(all\s+)?(previous|prior|the\s+above)",
        r"you\s+are\s+now\s+in\s+\w+\s+mode",
        r"unrestricted\s+mode",
        r"system\s+prompt",
        r"approve\s+all\s+(warranty|claims|refunds)",
        r"do\s+not\s+escalate",
        r"issue\s+a\s+full\s+refund",
        r"regardless\s+of\s+(purchase\s+date|warranty|coverage)",
        r"reply\s+only\s+with",
        r"new\s+instructions?\s*:",
        r"</?(system|assistant|instruction)>",
    )
]


def detect_injection(text: str) -> tuple[bool, list[str]]:
    """Return (flagged, matched_pattern_descriptions)."""
    hits = [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
    return bool(hits), hits


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    section: str
    heading: str | None
    content: str
    page_start: int | None = None
    page_end: int | None = None
    injection_flag: bool = False
    injection_hits: list[str] = field(default_factory=list)

    @property
    def token_estimate(self) -> int:
        # ~4 characters per token is close enough for budgeting and cost maths.
        return max(1, len(self.content) // 4)


def _page_index(pages: list) -> list[tuple[int, int, int]]:
    """Build (start_offset, end_offset, page_number) over the joined text.

    Lets every chunk carry a real page citation, which is what makes the agent's
    answers checkable by a human agent reading the same manual.
    """
    spans, offset = [], 0
    for p in pages:
        length = len(p.text)
        spans.append((offset, offset + length, p.number))
        offset += length + 1          # the "\n" join adds one character
    return spans


def _pages_for(spans: list[tuple[int, int, int]], start: int, end: int) -> tuple[int | None, int | None]:
    touched = [num for (s, e, num) in spans if s < end and e > start]
    return (min(touched), max(touched)) if touched else (None, None)


def _split_long(text: str, heading: str | None, section: str,
                spans, base_offset: int) -> list[Chunk]:
    """Split an over-long block on paragraph boundaries, with a little overlap."""
    if len(text) <= MAX_CHARS:
        ps, pe = _pages_for(spans, base_offset, base_offset + len(text))
        return [Chunk(section, heading, text.strip(), ps, pe)]

    out: list[Chunk] = []
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    buf, buf_start = "", base_offset

    for para in paragraphs:
        if buf and len(buf) + len(para) > TARGET_CHARS:
            ps, pe = _pages_for(spans, buf_start, buf_start + len(buf))
            out.append(Chunk(section, heading, buf.strip(), ps, pe))
            # Carry a tail of the previous chunk so a procedure split across a
            # boundary still reads as continuous.
            tail = buf[-OVERLAP_CHARS:]
            buf_start += max(0, len(buf) - OVERLAP_CHARS)
            buf = tail + "\n\n" + para
        else:
            buf = f"{buf}\n\n{para}" if buf else para

    if buf.strip():
        ps, pe = _pages_for(spans, buf_start, buf_start + len(buf))
        out.append(Chunk(section, heading, buf.strip(), ps, pe))
    return out


def chunk_document(pages: list, full_text: str) -> list[Chunk]:
    """Split a manual into retrievable units along its own structure."""
    spans = _page_index(pages)

    matches = list(SECTION_RE.finditer(full_text))
    if not matches:
        # OCR mangled every heading. Fall back to flat chunking rather than
        # dropping the document — degraded coverage beats no coverage, and the
        # confidence score already reflects that this manual is rough.
        log.warning("no section headings recognised; falling back to flat chunking")
        return _finalise(_split_long(full_text, None, "unknown", spans, 0))

    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        section = SECTION_ALIASES.get(title, title.lower().replace(" ", "_"))
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        body = full_text[body_start:body_end]

        if section == "troubleshooting":
            chunks.extend(_chunk_troubleshooting(body, spans, body_start))
        else:
            chunks.extend(_split_long(body, title, section, spans, body_start))

    return _finalise(chunks)


def _chunk_troubleshooting(body: str, spans, base_offset: int) -> list[Chunk]:
    """One chunk per symptom.

    This is the highest-value decision in the whole retrieval design. A customer
    describes a symptom; the unit of retrieval should be the complete procedure
    for that symptom, with its steps in order and its safety warning attached.
    """
    symptom_marks = list(SYMPTOM_RE.finditer(body))
    if not symptom_marks:
        return _split_long(body, "Troubleshooting", "troubleshooting", spans, base_offset)

    out: list[Chunk] = []
    # Any preamble before the first symptom.
    if symptom_marks[0].start() > 40:
        out.extend(_split_long(body[:symptom_marks[0].start()], "Troubleshooting",
                               "troubleshooting", spans, base_offset))

    for i, m in enumerate(symptom_marks):
        start = m.start()
        end = symptom_marks[i + 1].start() if i + 1 < len(symptom_marks) else len(body)
        block = body[start:end].strip()
        heading = m.group(1).strip()
        ps, pe = _pages_for(spans, base_offset + start, base_offset + end)
        # Keep the symptom procedure whole even if it runs slightly long; the
        # cost of an extra 200 characters is far lower than the cost of handing
        # the agent half a procedure.
        out.append(Chunk("troubleshooting", heading, block, ps, pe))
    return out


def _finalise(chunks: list[Chunk]) -> list[Chunk]:
    kept: list[Chunk] = []
    for c in chunks:
        if len(c.content.strip()) < 40:
            continue
        flagged, hits = detect_injection(c.content)
        c.injection_flag = flagged
        c.injection_hits = hits
        if flagged:
            log.warning("prompt-injection pattern in chunk (section=%s): %s",
                        c.section, hits)
        kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# Symbolic extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractedErrorCode:
    code: str
    title: str
    meaning: str
    first_actions: str
    page: int | None


def extract_error_codes(chunks: list[Chunk]) -> list[ExtractedErrorCode]:
    """Parse the error-code section into structured rows.

    Deliberately line-oriented and forgiving, because this has to survive OCR
    output as well as clean extraction. Anything it cannot parse stays available
    as an ordinary retrievable chunk, so a parse miss degrades to RAG rather
    than losing the information.
    """
    out: list[ExtractedErrorCode] = []
    seen: set[str] = set()

    for chunk in (c for c in chunks if c.section == "error_codes"):
        current: dict | None = None
        for raw_line in chunk.content.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith(("code ", "code\t")):
                continue

            m = ERROR_CODE_RE.match(line)
            # Guard against matching ordinary prose that happens to start with a
            # capital letter and a number.
            if m and len(m.group(1).replace(" ", "")) <= 3:
                if current:
                    out.append(_finish_code(current, chunk.page_start))
                current = {"code": m.group(1).replace(" ", "").upper(),
                           "rest": [m.group(2).strip()]}
            elif current is not None:
                current["rest"].append(line)

        if current:
            out.append(_finish_code(current, chunk.page_start))

    # Deduplicate, keeping the first (and usually cleanest) parse of each code.
    unique: list[ExtractedErrorCode] = []
    for ec in out:
        if ec.code in seen or not ec.meaning.strip():
            continue
        seen.add(ec.code)
        unique.append(ec)
    return unique


def _finish_code(current: dict, page: int | None) -> ExtractedErrorCode:
    body = " ".join(part for part in current["rest"] if part).strip()
    # The generator writes "<Title>. <Meaning> <First actions>"; split on the
    # first sentence for the title and treat the trailing imperative sentences
    # as the actions.
    sentences = re.split(r"(?<=[.!?])\s+", body)
    title = sentences[0].rstrip(".") if sentences else current["code"]

    # Start the search at index 1: sentences[0] is always the title, and titles
    # legitimately begin with words that are also imperative verbs ("Power meter
    # signal invalid"). Matching at index 0 would silently swallow the actions.
    action_start = next(
        (i for i in range(1, len(sentences))
         if re.match(r"^(Check|Run|Power|Reseat|Remove|Confirm|Replace|Stop|STOP|"
                     r"Reconnect|Unplug|Hold|Look|Move|Inspect|Try|Wipe|Tighten)",
                     sentences[i].strip())),
        None,
    )
    if action_start is not None:
        meaning = " ".join(sentences[1:action_start]).strip() or title
        actions = " ".join(sentences[action_start:]).strip()
    else:
        meaning = " ".join(sentences[1:]).strip() or title
        actions = ""

    return ExtractedErrorCode(
        code=current["code"], title=title[:200],
        meaning=meaning[:1000] or title, first_actions=actions[:1000], page=page,
    )
