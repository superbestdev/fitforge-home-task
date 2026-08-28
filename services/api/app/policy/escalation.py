"""Escalation triggers — deterministic.

An agent that decides for itself when to give up will keep going too long,
because "try one more thing" is always locally plausible. So the decision is
taken out of its hands: these are counters and thresholds evaluated in Python
after every turn.

Tracking *which* trigger fired is the most useful production signal the system
produces. A rise in `step_budget_exhausted` means the diagnostic prompts have
degraded; a rise in `no_coverage` means the manual corpus has a gap; a rise in
`tool_failures` means something downstream is broken. Same escalation rate, three
completely different responses. See docs/06-observability.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass
class EscalationCheck:
    escalate: bool
    reason: str | None = None
    detail: str | None = None
    # Shown to the customer at handoff. Written here rather than generated so
    # the promise made to the customer is always the one we can keep.
    customer_message: str | None = None


HANDOFF_MESSAGE = (
    "I have not been able to resolve this one, so I am passing you to a "
    "colleague. They will have everything we have already tried, so you will not "
    "need to repeat yourself."
)


def evaluate(
    *,
    safety_level: str = "ok",
    step_budget_used: int = 0,
    tool_failures: int = 0,
    coverage_status: str | None = None,
    best_retrieval_score: float | None = None,
    customer_requested_human: bool = False,
    frustration_signals: int = 0,
    quote_total_cents: int | None = None,
    restricted_part: bool = False,
) -> EscalationCheck:
    """Evaluate every escalation trigger. Order is by severity, not convenience."""

    if safety_level == "critical":
        return EscalationCheck(
            True, "safety",
            "Safety-critical phrase detected in the customer's description.",
            "I am connecting you to a person now.",
        )

    if restricted_part:
        return EscalationCheck(
            True, "restricted_part",
            "The indicated repair is technician-only.",
            "This repair needs a FitForge technician rather than a self-service "
            "part, so I am passing you to a colleague who can arrange it.",
        )

    if customer_requested_human:
        # Never argued with, never routed through a retention prompt.
        return EscalationCheck(
            True, "customer_request",
            "The customer asked for a human agent.",
            "Of course — connecting you to a colleague now.",
        )

    if coverage_status == "unbacked":
        # We have no manual for this model. Improvising here is exactly the
        # failure mode the coverage registry exists to prevent.
        return EscalationCheck(
            True, "no_coverage",
            "No usable service documentation is indexed for this model.",
            "I do not have the service documentation for your model to hand, and "
            "I would rather not guess. Passing you to a colleague who has it.",
        )

    if tool_failures >= settings.max_tool_failures_per_issue:
        return EscalationCheck(
            True, "tool_failures",
            f"{tool_failures} consecutive tool failures on this issue.",
            HANDOFF_MESSAGE,
        )

    if step_budget_used >= settings.diagnostic_step_budget:
        return EscalationCheck(
            True, "step_budget_exhausted",
            f"Diagnostic step budget of {settings.diagnostic_step_budget} used up "
            f"without reaching a resolution.",
            "We have worked through the checks I have for this symptom without "
            "finding it. Rather than keep going in circles, I am passing you to a "
            "colleague.",
        )

    if frustration_signals >= 2:
        return EscalationCheck(
            True, "customer_frustration",
            f"{frustration_signals} frustration signals in this session.",
            "I am sorry this has been frustrating. Let me get a person onto it.",
        )

    if (best_retrieval_score is not None
            and best_retrieval_score < settings.retrieval_min_score):
        # We found nothing that actually matches. The honest answer is that we
        # do not know, not a fluent paraphrase of the nearest unrelated section.
        return EscalationCheck(
            True, "low_retrieval_confidence",
            f"Best retrieval score {best_retrieval_score:.3f} is below the "
            f"{settings.retrieval_min_score} threshold.",
            "I cannot find guidance that matches what you are describing, and I "
            "do not want to send you down the wrong path. Passing you to a colleague.",
        )

    if (quote_total_cents is not None
            and quote_total_cents >= settings.payment_human_approval_cents):
        return EscalationCheck(
            True, "high_value_order",
            f"Quote total {quote_total_cents} cents is at or above the "
            f"{settings.payment_human_approval_cents} human-approval threshold.",
            "This one is above the amount I can process on my own, so a colleague "
            "will confirm it with you.",
        )

    return EscalationCheck(False)


FRUSTRATION_MARKERS = (
    "this is ridiculous", "useless", "waste of time", "not helping",
    "already told you", "i said", "for the third time", "again and again",
    "speak to a human", "real person", "let me talk to", "get me a human",
    "stop asking", "you keep asking", "terrible", "awful", "furious", "angry",
)

HUMAN_REQUEST_MARKERS = (
    "speak to a human", "talk to a human", "real person", "human agent",
    "get me a person", "transfer me", "speak to someone", "talk to someone",
    "agent please", "representative",
)


def detect_frustration(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in FRUSTRATION_MARKERS)


def detect_human_request(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in HUMAN_REQUEST_MARKERS)
