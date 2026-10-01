"""The overnight summary in words: what happened in the house between 11 pm
and 6 am, from Home Assistant's logbook.

Pure on purpose — no Home Assistant imports — so the words can be tested
on their own. overnight.py reads the logbook, the names and the dashboard's
config, calls in here, and stores what comes back.

Only the entities the dashboard is set up with are looked at: the room
lights (not camera illuminators), the doors, locks and alarm under
Security, each camera's motion sensor and doorbell, and the people on
Home. Every sentence here comes from those entries; the AI, when a home
has switched it on, only rewrites them (see ai_facts and accept_ai).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

# The order things are told in, most important first.
KINDS = ("alarm", "doorbell", "door", "lock", "person", "camera", "light")

_STATE_OF = re.compile(r"^state of ([a-z_]+\.[A-Za-z0-9_]+)")
_VOICE = {
    "homekit": "Apple Home",
    "alexa": "Alexa",
    "google_assistant": "Google Assistant",
    "conversation": "a voice command",
    "assist_satellite": "a voice command",
}
_ALARM_WORDS = {
    "armed_night": "set to Night",
    "armed_home": "set to Home",
    "armed_away": "set to Away",
    "armed_vacation": "set to Vacation",
    "armed_custom_bypass": "armed",
    "disarmed": "turned off",
    "triggered": "went off",
}


def clock(t: datetime) -> str:
    """'1:05 am', '11 pm'."""
    h = t.hour % 12 or 12
    ampm = "am" if t.hour < 12 else "pm"
    return f"{h}:{t.minute:02d} {ampm}" if t.minute else f"{h} {ampm}"


@dataclass
class Event:
    at: datetime
    kind: str
    entity_id: str
    name: str
    what: str
    why: str | None


def _what(kind: str, entity_id: str, state: str, last: str | None) -> str | None:
    domain = entity_id.split(".", 1)[0]
    if kind == "light":
        return state if state in ("on", "off") else None
    if kind == "door":
        if domain == "cover":
            return {"open": "opened", "opening": "opened", "closed": "closed"}.get(state)
        return {"on": "opened", "off": "closed"}.get(state)
    if kind == "lock":
        if domain == "lock":
            return {"unlocked": "unlocked", "open": "unlocked", "locked": "locked", "jammed": "jammed"}.get(state)
        # A lock's binary_sensor is on while unlocked.
        return {"on": "unlocked", "off": "locked"}.get(state)
    if kind == "alarm":
        return state if state in _ALARM_WORDS else None
    if kind == "camera":
        return "seen" if state == "on" else None
    if kind == "doorbell":
        return "rang" if state == "on" else None
    if kind == "person":
        if state in ("unknown", "unavailable"):
            return None
        if state == "home":
            return "arrived" if last != "home" else None
        return "left" if last in (None, "home") else None
    return None


def why(entry: dict[str, Any], names: dict[str, str], users: dict[str, str]) -> str | None:
    """Who or what did it, from the logbook's context — or None when the
    logbook doesn't say (a switch on the wall, a key in the door)."""
    kind = entry.get("context_event_type")
    if kind == "automation_triggered":
        automation = entry.get("context_name") or names.get(entry.get("context_entity_id") or "") or "an automation"
        source = entry.get("context_source") or ""
        cause = None
        if match := _STATE_OF.match(source):
            cause = names.get(match.group(1))
        elif source.startswith("sun event "):
            cause = source.removeprefix("sun event ")
        elif source.startswith("time"):
            cause = "a set time"
        return f"the “{automation}” automation" + (f", set off by {cause}" if cause else "")
    if kind == "script_started":
        return f"the “{entry.get('context_name') or 'a'}” script"
    if user := entry.get("context_user_id"):
        return users.get(user) or "someone in the app"
    if kind == "call_service":
        return _VOICE.get(entry.get("context_domain") or "", "a voice assistant or another app")
    if other := entry.get("context_entity_id"):
        return f"{names.get(other, other)} changing"
    return None


def events_of(
    entries: list[dict[str, Any]],
    catalog: dict[str, dict[str, str]],
    names: dict[str, str],
    users: dict[str, str],
    tz: tzinfo,
) -> list[Event]:
    """The logbook's entries as the night's events, oldest first: only the
    entities being watched, only the states that mean something, and each
    change once (a garage door's 'opening' then 'open' is one opening)."""
    out: list[Event] = []
    last_state: dict[str, str] = {}
    last_what: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: float(e.get("when") or 0)):
        entity_id = entry.get("entity_id")
        state = entry.get("state")
        item = catalog.get(entity_id or "")
        if not item or not isinstance(state, str) or state in ("unavailable", "unknown"):
            continue
        what = _what(item["kind"], entity_id, state, last_state.get(entity_id))
        last_state[entity_id] = state
        if what is None:
            # A camera going quiet: the next movement is news again.
            last_what.pop(entity_id, None)
            continue
        if last_what.get(entity_id) == what:
            continue
        last_what[entity_id] = what
        at = datetime.fromtimestamp(float(entry["when"]), tz)
        reason = why(entry, names, users) if item["kind"] in ("light", "lock", "alarm", "door") else None
        # A door that follows its own sensors isn't opened *by* them.
        if item["kind"] == "door" and reason and reason.endswith(" changing"):
            reason = None
        if item["kind"] == "light" and reason is None and what == "on":
            reason = "the switch"
        out.append(Event(at, item["kind"], entity_id, item["label"], what, reason))
    return out


def _span(times: list[datetime]) -> str:
    if len(times) == 1:
        return f"at {clock(times[0])}"
    return f"{len(times)} times between {clock(times[0])} and {clock(times[-1])}"


def _by(reason: str) -> str:
    return "at the switch" if reason == "the switch" else f"by {reason}"


def _reasons(events: list[Event]) -> str:
    """', by the “Night” automation', or the two most common reasons."""
    counts: OrderedDict[str, int] = OrderedDict()
    for e in events:
        if e.why:
            counts[e.why] = counts.get(e.why, 0) + 1
    ranked = sorted(counts, key=lambda k: -counts[k])
    if not ranked:
        return ""
    if len(ranked) == 1:
        return f", {_by(ranked[0])}"
    if counts[ranked[0]] >= 0.7 * len(events):
        return f", mostly {_by(ranked[0])}"
    return f", {_by(ranked[0])} and {_by(ranked[1])}"


def _by_entity(events: list[Event]) -> "OrderedDict[str, list[Event]]":
    groups: OrderedDict[str, list[Event]] = OrderedDict()
    for e in events:
        groups.setdefault(e.entity_id, []).append(e)
    return groups


def lines_of(events: list[Event]) -> list[str]:
    """One sentence per thing that happened, most important first."""
    lines: list[str] = []
    for kind in KINDS:
        for items in _by_entity([e for e in events if e.kind == kind]).values():
            name = items[0].name
            if kind == "alarm":
                for e in items:
                    by = f" by {e.why}" if e.why else ""
                    lines.append(f"The alarm {_ALARM_WORDS[e.what]} at {clock(e.at)}{by}.")
            elif kind == "doorbell":
                lines.append(f"The doorbell at {name} rang {_span([e.at for e in items])}.")
            elif kind == "door":
                opened = [e for e in items if e.what == "opened"]
                if not opened:
                    continue
                still = items[-1].what == "opened"
                if len(opened) == 1:
                    closed = next((e for e in items if e.what == "closed" and e.at > opened[0].at), None)
                    by = f" by {opened[0].why}" if opened[0].why else ""
                    end = " and was still open at 6 am" if still else (f" and closed at {clock(closed.at)}" if closed else "")
                    lines.append(f"{name} opened at {clock(opened[0].at)}{by}{end}.")
                else:
                    end = ", and was still open at 6 am" if still else ""
                    lines.append(f"{name} opened {_span([e.at for e in opened])}{end}.")
            elif kind == "lock":
                unlocked = [e for e in items if e.what == "unlocked"]
                jammed = [e for e in items if e.what == "jammed"]
                if unlocked:
                    first = unlocked[0]
                    by = f" by {first.why}" if first.why else ""
                    still = items[-1].what == "unlocked"
                    locked = next((e for e in items if e.what == "locked" and e.at > first.at), None)
                    if len(unlocked) == 1:
                        end = " and was still unlocked at 6 am" if still else (f" and locked again at {clock(locked.at)}" if locked else "")
                        lines.append(f"{name} was unlocked at {clock(first.at)}{by}{end}.")
                    else:
                        end = ", and was still unlocked at 6 am" if still else ""
                        lines.append(f"{name} was unlocked {_span([e.at for e in unlocked])}{end}.")
                if jammed:
                    lines.append(f"{name} jammed at {clock(jammed[0].at)}.")
            elif kind == "person":
                for e in items:
                    lines.append(f"{name} {'got home' if e.what == 'arrived' else 'left'} at {clock(e.at)}.")
            elif kind == "camera":
                lines.append(f"{name} saw movement {_span([e.at for e in items])}.")
            elif kind == "light":
                on = [e for e in items if e.what == "on"]
                if on:
                    lines.append(f"{name} came on {_span([e.at for e in on])}{_reasons(on)}.")
    return lines


def headline_of(events: list[Event]) -> str:
    """The one thing to know about the night."""
    def first(kind: str, what: str | None = None) -> Event | None:
        return next((e for e in events if e.kind == kind and (what is None or e.what == what)), None)

    if e := first("alarm", "triggered"):
        return f"The alarm went off at {clock(e.at)}."
    if e := first("doorbell"):
        return f"The doorbell rang at {clock(e.at)}."
    for kind, what, word in (("door", "opened", "open"), ("lock", "unlocked", "unlocked")):
        for items in _by_entity([x for x in events if x.kind == kind]).values():
            if items[-1].what == what:
                return f"{items[0].name} is still {word}."
    if e := first("door", "opened"):
        return f"{e.name} opened at {clock(e.at)}."
    if e := first("lock", "unlocked"):
        return f"{e.name} was unlocked at {clock(e.at)}."
    if e := first("person", "arrived"):
        return f"{e.name} got home at {clock(e.at)}."
    cameras = [x for x in events if x.kind == "camera"]
    if cameras:
        busiest = max(_by_entity(cameras).values(), key=len)
        return f"{busiest[0].name} saw movement {_span([x.at for x in busiest])}."
    lights = [x for x in events if x.kind == "light" and x.what == "on"]
    if lights:
        automatic = all(x.why and x.why.startswith("the “") for x in lights)
        return "A quiet night. Only the automatic lights came on." if automatic else "A quiet night. A few lights came on."
    return "All quiet overnight."


def summarize(
    entries: list[dict[str, Any]],
    catalog: dict[str, dict[str, str]],
    names: dict[str, str],
    users: dict[str, str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """The night as the dashboard stores and shows it: the headline, one
    line per thing that happened, and every event with its time and reason."""
    tz = start.tzinfo
    assert tz is not None
    events = events_of(entries, catalog, names, users, tz)
    return {
        "date": end.date().isoformat(),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "headline": headline_of(events),
        "lines": lines_of(events),
        "events": [
            {"at": e.at.isoformat(), "kind": e.kind, "name": e.name, "what": e.what, "why": e.why}
            for e in events[-300:]
        ],
        "ai": False,
    }


# ---- In Messages ----------------------------------------------------------------

MESSAGE_TITLE = "Last night, 11 pm to 6 am"


def message_of(record: dict[str, Any]) -> str:
    """The night as a Message Centre message: the headline (the AI's, with
    its few sentences, when it wrote one), then a line for each thing that
    happened."""
    parts = [str(record.get("aiHeadline") or record["headline"])]
    if record.get("aiSummary"):
        parts.append(str(record["aiSummary"]))
    if record.get("lines"):
        parts.append("\n".join(f"• {line}" for line in record["lines"]))
    return "\n\n".join(parts)


# ---- The AI ---------------------------------------------------------------------

def ai_facts(record: dict[str, Any]) -> str:
    return "\n".join([f"Last night, 11 pm to 6 am. {record['headline']}", *record["lines"]]) if record["lines"] else record["headline"]


def ai_request(record: dict[str, Any]) -> dict[str, Any]:
    """What Home Assistant's AI Task is asked: the facts, the rules, and the
    two fields to fill."""
    instructions = "\n".join(
        [
            "You write the morning summary of the night on a family’s home dashboard in Australia.",
            "Using ONLY the facts below, say what happened overnight, most important first.",
            "Never invent anything: no times, names, reasons or events that aren’t in the facts, and keep every time exactly as given.",
            "Plain, calm, friendly Australian English. Short sentences. Don’t alarm them over automatic lights.",
            "",
            "FACTS",
            ai_facts(record),
        ]
    )
    structure = {
        "headline": {"description": "The one thing to know about the night, max 12 words.", "required": True, "selector": {"text": {}}},
        "summary": {"description": "2 or 3 sentences about the night, from the facts.", "required": True, "selector": {"text": {}}},
    }
    return {"task_name": "EverRise overnight summary", "instructions": instructions, "structure": structure}


_NUMBER = re.compile(r"\d+(?:[.:]\d+)?")


def accept_ai(data: Any, record: dict[str, Any]) -> dict[str, str] | None:
    """The AI's words, when they pass: both fields there, a sensible length,
    and no number that isn't in the facts."""
    if not isinstance(data, dict):
        return None
    allowed = set(_NUMBER.findall(ai_facts(record)))
    out: dict[str, str] = {}
    for key, limit in (("headline", 110), ("summary", 600)):
        text = data.get(key)
        if not isinstance(text, str):
            return None
        text = " ".join(text.split())
        if len(text) < 3 or len(text) > limit or not set(_NUMBER.findall(text)) <= allowed:
            return None
        out[key] = text
    return out
