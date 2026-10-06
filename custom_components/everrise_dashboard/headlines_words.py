"""The words around Home's AI headlines (headlines.py): what the home's AI
is asked, the shape its answer must take, and the checks that keep only a
short, kind line. No Home Assistant here, so it can be tested alone.

Nothing about the home goes into the ask: no names, rooms, devices, states
or place. The AI only gets the three general moments the Home page has a
plain all's-well headline for, and the last few lines it wrote, so the new
ones are different.
"""

from __future__ import annotations

import re
from typing import Any

TASK_NAME = "EverRise home headline"

# The moments Home words with the AI, in the order they're asked for, and
# what each one means. Alerts and "Garage closed."-style lines never come
# from the AI: they stay the dashboard's own words.
MOMENTS: dict[str, str] = {
    "calm": "Someone is home and everything is fine.",
    "away": "Nobody is home, and the house is locked up with the alarm on.",
    "night": "It's night-time, the alarm is on and the house is settling down for sleep.",
}

ROLE = (
    "You write the big headline on a family's smart-home screen. "
    "Only give advice. Do not control, change or turn on anything in the house."
)
TASK = (
    "Write one new headline for each moment below. It replaces a plain line like \"All good.\" in big "
    "letters, so keep it short: at most 6 words.\n\n"
    "{moments}\n\n"
    "Make them warm and playfully funny: the kind of gentle joke the whole family smiles at. Never rude, "
    "mean, sarcastic, crude or scary, and nothing about burglars, crime, danger, money, health, politics or "
    "religion. No names, numbers, emojis, hashtags or quotation marks, and don't mention any person, pet, "
    "room, device or brand. Nothing may suggest that something is wrong or needs doing. Use Australian "
    "English.{recent}"
)

# The longest line kept, in characters and words: a phone shows it in big
# type, two lines at most.
MAX_CHARS = 48
MAX_WORDS = 8
# How many earlier lines the AI is shown so it doesn't repeat itself.
RECENT_KEPT = 9

# Home Assistant's AI Task answers in these parts (ai_task.generate_data's
# `structure`).
STRUCTURE: dict[str, Any] = {
    key: {"description": f"The headline for: {meaning}", "required": True, "selector": {"text": {}}}
    for key, meaning in MOMENTS.items()
}

# Words a headline is thrown away for: unkind, crude, or the kind of thing
# nobody wants to read on their alarm's home screen.
_BLOCKED = re.compile(
    r"\b(?:burglar\w*|thie(?:f|ves)|robb\w*|crim\w*|intrud\w*|break-?ins?|steal\w*|stolen|danger\w*|unsafe|"
    r"emergenc\w*|fire|smoke|police|kill\w*|die[sd]?|dead|death|hate\w*|stupid\w*|idiot\w*|dumb\w*|ugly|fat|"
    r"shut up|damn\w*|hell|crap\w*|shit\w*|fuck\w*|bitch\w*|bastard\w*|arse\w*|ass|asses|bloody|sex\w*|"
    r"drunk\w*|booze\w*)\b",
    re.IGNORECASE,
)
_MARKS = re.compile(r"\*+|`+|^\s*#+\s*|^\s*[-•]\s+", re.MULTILINE)
# Emoji and other pictographs, which the Home headline doesn't use.
_PICTURES = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_QUOTES = "\"'“”‘’«»"
_SPACE = re.compile(r"\s+")


def instructions_of(recent: list[str]) -> str:
    """What the AI is asked: the moments, and the lines it wrote last time."""
    moments = "\n".join(f"- {key}: {meaning}" for key, meaning in MOMENTS.items())
    shown = [line.strip().rstrip('.!?') for line in recent if isinstance(line, str) and line.strip()][-RECENT_KEPT:]
    said = f" Make each one different from these earlier ones: {'; '.join(shown)}." if shown else ""
    return f"{ROLE}\n\n{TASK.format(moments=moments, recent=said)}"


def clean_line(value: Any) -> str | None:
    """One kind, short headline on one line, or None when the AI's line is
    too long, has numbers, links or tags, or says anything on the blocked
    list."""
    if not isinstance(value, str):
        return None
    text = _SPACE.sub(" ", _PICTURES.sub("", _MARKS.sub("", value))).strip()
    text = text.strip(_QUOTES + " ").strip()
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return None
    if re.search(r"\d|@|#|https?:|www\.", text, re.IGNORECASE) or _BLOCKED.search(text):
        return None
    return text


def accept_headlines(data: Any) -> dict[str, str] | None:
    """All three headlines, each passing clean_line; None when any of them
    is missing or fails it, so a half-usable answer never replaces the last
    good one."""
    if not isinstance(data, dict):
        return None
    lines = {key: clean_line(data.get(key)) for key in MOMENTS}
    if not all(lines.values()):
        return None
    return {key: line for key, line in lines.items() if line}
