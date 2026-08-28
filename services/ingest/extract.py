"""PDF text extraction and born-digital vs scanned classification.

Uses pypdfium2 (BSD-3 / Apache-2.0). PyMuPDF would be a slightly nicer API but
it is AGPL, which is a licensing hazard for a commercial support product — see
docs/03-tech-stack.md. This is the kind of decision that is cheap to make now
and expensive to undo after legal review.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pypdfium2 as pdfium

log = logging.getLogger(__name__)

# Below this many extractable characters, a page is treated as an image. Real
# scanned pages usually yield 0, but a page can carry a stray text artefact, so
# the threshold is not zero.
TEXT_DENSITY_THRESHOLD = 60


@dataclass
class PageText:
    number: int          # 1-indexed
    text: str
    char_count: int
    is_image_only: bool


@dataclass
class ExtractResult:
    pages: list[PageText]
    page_count: int
    image_only_pages: int

    @property
    def needs_ocr(self) -> bool:
        """True when enough pages lack a text layer to be worth OCRing."""
        if self.page_count == 0:
            return False
        return self.image_only_pages / self.page_count > 0.3

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


def _clean(raw: str) -> str:
    """Normalise extractor output without destroying structure.

    Line breaks matter here: the chunker splits on section headings and the
    error-code parser works line by line, so we collapse horizontal whitespace
    but keep vertical whitespace intact.
    """
    # Ligatures and the mojibake pdfium sometimes emits for them.
    raw = (raw.replace("ﬁ", "fi").replace("ﬂ", "fl")
              .replace("’", "'").replace("‘", "'")
              .replace("“", '"').replace("”", '"')
              .replace("–", "-").replace("—", " - ")
              .replace(" ", " "))
    # De-hyphenate words broken across a line.
    raw = re.sub(r"(\w)-\n(\w)", r"\1\2", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def extract_pdf(path: str | bytes) -> ExtractResult:
    """Pull per-page text out of a PDF."""
    doc = pdfium.PdfDocument(path)
    try:
        pages: list[PageText] = []
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            try:
                raw = textpage.get_text_bounded()
            finally:
                textpage.close()

            text = _clean(raw or "")
            count = len(text)
            pages.append(PageText(
                number=i + 1,
                text=text,
                char_count=count,
                is_image_only=count < TEXT_DENSITY_THRESHOLD,
            ))

        return ExtractResult(
            pages=pages,
            page_count=len(pages),
            image_only_pages=sum(1 for p in pages if p.is_image_only),
        )
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

# A rough dictionary check. OCR failure shows up as a collapse in the ratio of
# recognisable words, and that ratio is what we propagate as ingest_confidence.
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_COMMON = {
    "the", "and", "for", "with", "that", "this", "from", "not", "you", "your",
    "is", "are", "was", "will", "can", "check", "unit", "belt", "power", "if",
    "the", "manual", "service", "part", "parts", "model", "section", "warranty",
    "replace", "remove", "install", "inspect", "cable", "motor", "console",
    "screen", "seat", "error", "code", "months", "purchase", "customer",
}


def text_quality(text: str) -> float:
    """Score 0.0-1.0 for how much like real English the extracted text looks.

    Cheap, dependency-free, and good enough to separate a clean OCR pass from a
    garbled one. Its job is not to be precise — it is to make low-confidence
    content visibly low-confidence downstream, so the agent leans on citations
    and escalates sooner for those models.
    """
    words = _WORD_RE.findall(text.lower())
    if len(words) < 30:
        return 0.0

    # Signal 1: how many tokens are plausible-length words.
    plausible = sum(1 for w in words if 2 <= len(w) <= 14)
    len_score = plausible / len(words)

    # Signal 2: do we see the common English words we would expect in a manual?
    hits = sum(1 for w in words if w in _COMMON)
    common_score = min(1.0, (hits / len(words)) / 0.06)

    # Signal 3: OCR garbage is dense in isolated single characters.
    singles = sum(1 for w in _WORD_RE.findall(text) if len(w) == 1)
    single_penalty = max(0.0, 1.0 - (singles / max(1, len(words))) * 4)

    return round(
        max(0.0, min(1.0, 0.35 * len_score + 0.45 * common_score + 0.20 * single_penalty)),
        3,
    )
