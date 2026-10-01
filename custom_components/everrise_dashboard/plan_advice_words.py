"""The words around the energy plan's advice (plan_advice.py): what the
home's AI is asked, the shape its answer must take, and the check that keeps
only a usable answer. No Home Assistant here, so it can be tested alone.

The dashboard sends the house's numbers as plain text (lib/adviceFacts.ts in
the dashboard repo): what the sun, the battery, the house and the grid are
doing, the power prices for the coming hours, the car, the appliances and
the weather. No names, addresses or entity ids.
"""

from __future__ import annotations

import re
from typing import Any

TASK_NAME = "EverRise energy plan"
# The house's numbers come to a few thousand characters; anything far
# bigger isn't them.
MAX_FACTS = 12_000

ROLE = (
    "You are advising a household in {place} on their home energy. "
    "Only give advice. Do not control, change or turn on anything in the house."
)
TASK = (
    "Use the weather forecast as well as the prices. Think about how the cloud affects the solar, "
    "and whether the sun in the coming days will refill the battery, and let that shape your advice "
    "for the appliances, the car and the battery tonight.\n\n"
    "Using only these numbers, tell the household what to do. Write plain sentences, with no "
    "markdown and no lists, and keep each part short."
)

# Each part of the answer: what it's for, whether it must be there, and the
# most characters kept.
FIELDS: dict[str, tuple[str, bool, int]] = {
    "headline": ("A one-line headline for the next hour.", True, 200),
    "appliances": (
        "Washer, dishwasher and dryer: run now, or wait until when, and roughly what it costs or saves. "
        "One to three sentences.",
        True,
        600,
    ),
    "car": (
        "The car: keep charging now, or stop and charge later, and why. One to three sentences. "
        "Leave it empty if there's no car or it's away.",
        False,
        600,
    ),
    "battery": ("This evening and overnight: anything worth doing with the battery. One to three sentences.", True, 600),
    "weather": ("One sentence on how the weather changed this advice.", True, 300),
}

# Home Assistant's AI Task answers in these parts (ai_task.generate_data's
# `structure`).
STRUCTURE: dict[str, Any] = {
    key: {"description": description, "required": required, "selector": {"text": {}}}
    for key, (description, required, _limit) in FIELDS.items()
}

_MARKS = re.compile(r"\*+|`+|^\s*#+\s*|^\s*[-•]\s+", re.MULTILINE)
_SPACE = re.compile(r"\s+")


def place_of(time_zone: str | None) -> str:
    """Home Assistant's time zone as a place: 'Australia/Sydney' is Sydney."""
    if not time_zone or "/" not in time_zone:
        return "Australia"
    return time_zone.rsplit("/", 1)[1].replace("_", " ")


def instructions_of(facts: str, place: str) -> str:
    """What the AI is asked: who it's advising, the house's numbers, then the task."""
    return f"{ROLE.format(place=place)}\n\n{facts.strip()}\n\n{TASK}"


def clean(text: str, limit: int) -> str:
    """Plain words on one line, with no markdown; cut at a sentence when long."""
    words = _SPACE.sub(" ", _MARKS.sub("", text)).strip()
    if len(words) <= limit:
        return words
    cut = words[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


def accept_advice(data: Any) -> dict[str, str] | None:
    """The answer's parts as plain text; None without a headline and the
    appliances, which every answer has to have."""
    if not isinstance(data, dict):
        return None
    advice: dict[str, str] = {}
    for key, (_description, _required, limit) in FIELDS.items():
        value = data.get(key)
        if isinstance(value, str) and (text := clean(value, limit)):
            advice[key] = text
    return advice if advice.get("headline") and advice.get("appliances") else None
