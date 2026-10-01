"""The overnight summary: at 6 am, what happened in the house between 11 pm
and 6 am, from Home Assistant's logbook.

Written in the house every morning (overnight_words.py), from only the
entities the dashboard is set up with. A household that switches it on in
Admin → Home also has the home's AI (Home Assistant's AI Task, the same
Gemini the energy plan uses) rewrite it; its words are kept only when every
number in them is in the facts, and our own stay otherwise. Nothing about
the night leaves the house unless that switch is on.

Kept for KEEP_NIGHTS nights under this integration's own storage (never in
www/), and read by the dashboard through the authenticated API in
overnight_http.py: Home shows the morning's until 9 am. It also goes into
the Message Centre, where it stays to be read later, as one message per
night (written again, not twice, if the night is summarized again). Runs
once at 6:00:30, again after a restart during the morning if it missed
6 am, and whenever `everrise_dashboard.summarize_night` is called.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import CONF_FILENAME, CONF_FOLDER, DEFAULT_FILENAME, DEFAULT_FOLDER, DOMAIN
from .notifications_store import put_notification
from .overnight_words import MESSAGE_TITLE, accept_ai, ai_request, message_of, summarize
from .storage import base_dir, read_json, resolve_config_path, write_json_atomic

_LOGGER = logging.getLogger(__name__)

SERVICE_SUMMARIZE_NIGHT = "summarize_night"
START_HOUR = 23
END_HOUR = 6
# A restart after 6 am and before this still writes the morning's summary
# (Home shows it until 9 am; Messages keeps it).
CATCH_UP_UNTIL_HOUR = 12
KEEP_NIGHTS = 14
AI_TIMEOUT_S = 90
_STATE_OF = re.compile(r"^state of ([a-z_]+\.[A-Za-z0-9_]+)")


def _store_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / "overnight" / "nights.json"


def read_nights(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Newest first; empty before the first morning."""
    path = _store_path(hass)
    if not path.exists():
        return []
    data = read_json(path)
    return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []


def _save_night(hass: HomeAssistant, record: dict[str, Any]) -> None:
    nights = [n for n in read_nights(hass) if n.get("date") != record["date"]]
    nights.append(record)
    nights.sort(key=lambda n: str(n.get("date", "")), reverse=True)
    write_json_atomic(_store_path(hass), nights[:KEEP_NIGHTS])


def _read_config(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    path = resolve_config_path(
        hass, entry.options.get(CONF_FOLDER, DEFAULT_FOLDER), entry.options.get(CONF_FILENAME, DEFAULT_FILENAME)
    )
    if path is None or not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def catalog_of(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    """What to watch, by entity: the room lights (not what the dashboard
    already leaves out, like camera illuminators), the doors, locks and
    alarm, each camera's motion sensor and doorbell, and the people."""
    out: dict[str, dict[str, str]] = {}

    def add(entity_id: Any, kind: str, label: Any) -> None:
        if isinstance(entity_id, str) and "." in entity_id and entity_id not in out:
            out[entity_id] = {"kind": kind, "label": label if isinstance(label, str) and label.strip() else entity_id}

    lights = cfg.get("lights") if isinstance(cfg.get("lights"), dict) else {}
    exclude = [p for p in lights.get("excludePatterns") or [] if isinstance(p, str) and p]
    for room in lights.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        room_name = room.get("name") if isinstance(room.get("name"), str) else ""
        for light in room.get("lights") or []:
            if not isinstance(light, dict) or any(p in str(light.get("entityId")) for p in exclude):
                continue
            label = light.get("label") if isinstance(light.get("label"), str) else ""
            name = label if room_name.lower() in label.lower() else f"{room_name} {label.lower()}".strip()
            add(light.get("entityId"), "light", name or room_name)

    security = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
    for door in security.get("doors") or []:
        if isinstance(door, dict):
            add(door.get("entityId"), "door", door.get("label"))
    for lock in security.get("locks") or []:
        if isinstance(lock, dict):
            add(lock.get("entityId"), "lock", lock.get("label"))
    add(security.get("alarmEntityId"), "alarm", "Alarm")

    cameras = cfg.get("cameras") if isinstance(cfg.get("cameras"), dict) else {}
    motion = cameras.get("motionSensors") if isinstance(cameras.get("motionSensors"), dict) else {}
    for camera in [*(cameras.get("all") or []), *(cameras.get("preview") or [])]:
        if not isinstance(camera, dict):
            continue
        label = camera.get("label") if isinstance(camera.get("label"), str) else "A camera"
        add(motion.get(camera.get("entityId")), "camera", label if "camera" in label.lower() else f"{label} camera")
        add(camera.get("ringEntityId"), "doorbell", label)

    for person in cfg.get("people") or []:
        if isinstance(person, dict):
            add(person.get("entityId"), "person", person.get("label"))
    return out


async def _logbook(hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The logbook's entries for these entities, with who or what caused
    each — the same query the logbook page runs."""
    if "logbook" not in hass.config.components or "recorder" not in hass.config.components:
        _LOGGER.warning("The overnight summary needs Home Assistant's logbook and recorder")
        return []
    # Imported here so an install without the logbook still loads.
    from homeassistant.components.logbook.helpers import async_determine_event_types, async_filter_entities
    from homeassistant.components.logbook.processor import EventProcessor
    from homeassistant.components.recorder import get_instance

    ids = async_filter_entities(hass, entity_ids)
    if not ids:
        return []
    event_types = async_determine_event_types(hass, ids, None)
    processor = EventProcessor(hass, event_types, ids, None, None, timestamp=True, include_entity_name=False)
    return await get_instance(hass).async_add_executor_job(
        processor.get_events, dt_util.as_utc(start), dt_util.as_utc(end)
    )


def _names(hass: HomeAssistant, entries: list[dict[str, Any]]) -> dict[str, str]:
    """Friendly names for whatever set things off."""
    wanted: set[str] = set()
    for entry in entries:
        if match := _STATE_OF.match(str(entry.get("context_source") or "")):
            wanted.add(match.group(1))
        if isinstance(entry.get("context_entity_id"), str):
            wanted.add(entry["context_entity_id"])
    names: dict[str, str] = {}
    for entity_id in wanted:
        state = hass.states.get(entity_id)
        if state and isinstance(state.attributes.get("friendly_name"), str):
            names[entity_id] = state.attributes["friendly_name"]
    return names


async def _users(hass: HomeAssistant, entries: list[dict[str, Any]]) -> dict[str, str]:
    users: dict[str, str] = {}
    for user_id in {e["context_user_id"] for e in entries if isinstance(e.get("context_user_id"), str)}:
        user = await hass.auth.async_get_user(user_id)
        if user and user.name:
            users[user_id] = user.name
    return users


def _pick_ai(hass: HomeAssistant) -> str | None:
    """The home's AI: Google's when there is one, as the energy plan picks."""
    ids = sorted(s.entity_id for s in hass.states.async_all("ai_task") if s.state != "unavailable")
    return next((i for i in ids if re.search("google|gemini", i)), ids[0] if ids else None)


async def _ai_words(hass: HomeAssistant, record: dict[str, Any]) -> dict[str, str] | None:
    entity_id = _pick_ai(hass)
    if entity_id is None:
        return None
    try:
        async with asyncio.timeout(AI_TIMEOUT_S):
            response = await hass.services.async_call(
                "ai_task",
                "generate_data",
                {"entity_id": entity_id, **ai_request(record)},
                blocking=True,
                return_response=True,
            )
    except Exception as err:  # noqa: BLE001 - any failure keeps our own words
        _LOGGER.warning("Overnight summary: the AI couldn't write it (%s), so it's in our own words", err)
        return None
    data = response.get("data") if isinstance(response, dict) else None
    words = accept_ai(data, record)
    if words is None:
        _LOGGER.info("Overnight summary: the AI's words didn't match the night, so it's in our own words")
    return words


def night_window(now: datetime) -> tuple[datetime, datetime]:
    """The last whole night before `now`: 11 pm to 6 am, local time."""
    end = now.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    if now < end:
        end -= timedelta(days=1)
    start = (end - timedelta(days=1)).replace(hour=START_HOUR)
    return start, end


async def async_summarize_night(hass: HomeAssistant, entry: ConfigEntry, now: datetime | None = None) -> dict[str, Any]:
    """Write, keep and return the summary of the last whole night."""
    start, end = night_window(now or dt_util.now())
    cfg = await hass.async_add_executor_job(_read_config, hass, entry)
    catalog = catalog_of(cfg)
    entries = await _logbook(hass, list(catalog), start, end) if catalog else []
    record = summarize(entries, catalog, _names(hass, entries), await _users(hass, entries), start, end)
    record["generatedAt"] = dt_util.now().isoformat()
    home = cfg.get("home") if isinstance(cfg.get("home"), dict) else {}
    if home.get("lastNightAi") is True and record["lines"]:
        if words := await _ai_words(hass, record):
            record.update({"ai": True, "aiHeadline": words["headline"], "aiSummary": words["summary"]})
    await hass.async_add_executor_job(_save_night, hass, record)
    await hass.async_add_executor_job(
        put_notification, hass, f"overnight-{record['date']}", MESSAGE_TITLE, message_of(record)
    )
    return record


def _current_entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


async def async_setup_overnight(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """6 am every morning, a catch-up after a restart during the morning,
    and the service to write it now."""

    async def _run(_now: datetime | None = None) -> None:
        current = _current_entry(hass)
        if current is None:
            return
        try:
            await async_summarize_night(hass, current)
        except Exception:  # noqa: BLE001 - one bad morning mustn't stop the next
            _LOGGER.exception("Couldn't write the overnight summary")

    entry.async_on_unload(async_track_time_change(hass, _run, hour=END_HOUR, minute=0, second=30))

    async def _catch_up(_now: datetime) -> None:
        now = dt_util.now()
        if not END_HOUR <= now.hour < CATCH_UP_UNTIL_HOUR:
            return
        nights = await hass.async_add_executor_job(read_nights, hass)
        if not any(n.get("date") == now.date().isoformat() for n in nights):
            await _run()

    # After the recorder has settled from a restart.
    entry.async_on_unload(async_call_later(hass, 120, _catch_up))

    if hass.data[DOMAIN].get("overnight_service_registered"):
        return

    async def _handle(call: ServiceCall) -> ServiceResponse:
        current = _current_entry(hass)
        if current is None:
            return {}
        return await async_summarize_night(hass, current)

    hass.services.async_register(DOMAIN, SERVICE_SUMMARIZE_NIGHT, _handle, supports_response=SupportsResponse.OPTIONAL)
    hass.data[DOMAIN]["overnight_service_registered"] = True
