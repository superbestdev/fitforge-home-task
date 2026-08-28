"""Safety gating — deterministic, and it runs before the model does.

The single most dangerous thing this system can do is talk a customer through
opening a mains-voltage motor housing, or tell them a frayed cable under a
loaded weight stack is fine to keep using. Those are not judgement calls we
delegate to a 4B parameter model running on a CPU.

So safety is checked twice, in plain Python:

  * on the way in, against the customer's own words, before any LLM call
  * on the way out, against any part or procedure the agent wants to propose

Both checks fail closed: when they fire, the session escalates and no amount of
customer insistence reopens the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that mean "stop and get a human", regardless of context. Kept as
# explicit strings rather than a classifier because a false negative here is a
# safety incident and a false positive is merely a handoff.
CRITICAL_PHRASES = (
    "smoke", "smoking", "on fire", "fire", "flames", "sparks", "sparking",
    "arcing",
    # Burning smells, in every phrasing customers actually use. Listing them out
    # rather than matching bare "burning" is deliberate — see BENIGN_BURN below.
    "burning smell", "smell burning", "smell of burning", "smells like burning",
    "smells burning", "burnt smell", "smell burnt", "smells burnt",
    "smell of smoke", "smells of smoke", "smell smoke", "acrid",
    "electric shock", "electrocuted", "shocked me", "got a shock",
    "melting", "melted", "too hot to touch", "smoldering", "smouldering",
    "frayed cable", "frayed wire", "broken strand", "cable snapped",
    "cable is fraying", "wire sticking out",
    "injured", "injury", "bleeding", "broke my", "hurt myself",
    "fell off", "trapped", "caught my hand", "crushed",
)

# This is fitness equipment, so "burn" is an everyday word here in a completely
# harmless sense. Any critical match whose only trigger overlaps one of these is
# discarded — otherwise "I burned 400 calories then it stopped" escalates.
BENIGN_BURN = (
    "burning calories", "calories burning", "calorie burn", "burn calories",
    "calories burned", "fat burning", "burning fat", "burn rate",
    "burning up the miles", "feel the burn", "muscles burning",
    "legs are burning", "burning sensation in my",
)

# Slightly softer: warrants a warning and a lower escalation threshold, but not
# an immediate stop.
CAUTION_PHRASES = (
    "breaker keeps tripping", "keeps tripping", "shorted", "short circuit",
    "smells hot", "very hot", "loud bang", "grinding loudly",
    "child", "toddler", "kid got",
)

# Never talk a customer through fitting these, whatever they ask.
RESTRICTED_PART_CLASSES = ("electronics",)


@dataclass
class SafetyVerdict:
    level: str                 # 'ok' | 'caution' | 'critical'
    triggered_by: list[str]
    message: str | None = None

    @property
    def blocks_troubleshooting(self) -> bool:
        return self.level == "critical"


def _find(text: str, phrases: tuple[str, ...]) -> list[str]:
    low = re.sub(r"\s+", " ", text.lower())
    return [p for p in phrases if p in low]


def screen_customer_message(text: str) -> SafetyVerdict:
    """Screen inbound customer text before it reaches the model."""
    low = re.sub(r"\s+", " ", text.lower())

    critical = _find(text, CRITICAL_PHRASES)
    # Drop matches that are only present because of an everyday fitness phrase.
    if critical and any(b in low for b in BENIGN_BURN):
        critical = [c for c in critical
                    if not any(c in b for b in BENIGN_BURN if b in low)]

    if critical:
        return SafetyVerdict(
            level="critical",
            triggered_by=critical,
            message=(
                "Please stop using the machine now and unplug it at the wall. "
                "What you are describing needs a FitForge technician rather than "
                "step-by-step troubleshooting, and I am connecting you to a "
                "person straight away."
            ),
        )

    caution = _find(text, CAUTION_PHRASES)
    if caution:
        return SafetyVerdict(
            level="caution",
            triggered_by=caution,
            message="Let us be careful with this one — stop using the machine "
                    "until we understand what is happening.",
        )

    return SafetyVerdict(level="ok", triggered_by=[])


def screen_part_for_self_service(part: dict, category_safety_class: str) -> SafetyVerdict:
    """Decide whether we may walk a customer through fitting a part themselves."""
    if part.get("safety_class") == "restricted" or not part.get("customer_replaceable", True):
        return SafetyVerdict(
            level="critical",
            triggered_by=[f"restricted_part:{part.get('part_number')}"],
            message=(
                f"{part.get('name')} is not a customer-fitted part. Fitting it "
                "yourself is unsafe and would end the remaining coverage on the "
                "machine, so this needs a FitForge technician."
            ),
        )

    if (category_safety_class == "high_voltage"
            and part.get("part_class") in RESTRICTED_PART_CLASSES):
        return SafetyVerdict(
            level="critical",
            triggered_by=[f"high_voltage_electronics:{part.get('part_number')}"],
            message=(
                "That repair means working inside a mains-voltage enclosure, "
                "which we never ask customers to do. A technician will handle it."
            ),
        )

    return SafetyVerdict(level="ok", triggered_by=[])


def category_safety_preamble(category_safety_class: str) -> str:
    """Standing constraints injected into the diagnostic prompt for a category."""
    return {
        "high_voltage": (
            "SAFETY (mains voltage): Never instruct the customer to remove the "
            "motor hood, controller cover, or any panel with tamper-proof screws. "
            "Never instruct them to work on the machine while it is plugged in. "
            "If they report smoke, burning, sparks, shocks, or a repeatedly "
            "tripping breaker, stop troubleshooting and escalate."
        ),
        "high_tension": (
            "SAFETY (high tension): Cables and pulleys store dangerous energy. "
            "Never instruct the customer to replace, re-route, or tension a "
            "cable, or to work on the weight stack. Any reported cable damage "
            "means the machine goes out of service and the session escalates."
        ),
        "standard": (
            "SAFETY: Keep all guidance to externally accessible adjustments and "
            "customer-replaceable parts. Do not instruct the customer to open "
            "sealed enclosures."
        ),
    }.get(category_safety_class, "")
