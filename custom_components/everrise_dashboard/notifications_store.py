"""Persisted notification history for the dashboard's Message Centre.

Separate from the automation builder's existing "attach a snapshot" notify
option (see the dashboard repo's automations.ts / EventPage.tsx): that
feature answers "what caused the push I just tapped" from a fresh
notification, with nothing kept afterward — no list, no history, and its
snapshot lands in the public, unauthenticated `www/everrise-dashboard/events/`
folder because it only ever needs to survive long enough for one tap.

This module is what backs `everrise_dashboard.log_notification`, a service
any automation can call (alongside its own notify.mobile_app_* call) to
record that a notification happened — title, message, optional target label,
and an optional camera snapshot — in bridge-managed storage, kept for
NOTIFICATION_RETENTION_DAYS and only reachable through the authenticated
HTTP API in notifications_http.py. That's what lets the dashboard show a
browsable Message Centre days later, not just react to the one push that
just arrived.

Same on-disk conventions as the rest of this integration: everything lives
under base_dir(hass) (see storage.py), writes are atomic (temp file +
os.replace), and a missing index is an empty list rather than an error —
this only ever gets used after `log_notification` is called for the first
time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

from .const import DOMAIN, NOTIFICATIONS_SUBDIR, NOTIFICATION_RETENTION_DAYS
from .storage import base_dir

_LOGGER = logging.getLogger(__name__)

SERVICE_LOG_NOTIFICATION = "log_notification"

LOG_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("target"): cv.string,
        vol.Optional("camera_entity_id"): cv.entity_id,
        # Lets the caller pick the id this record (and its image, if any)
        # is saved under — see buildLogNotificationBlock in the dashboard's
        # automations.ts, which computes one deterministic id per notify
        # action from a shared per-firing timestamp variable, so it can
        # point that SAME action's notify.* clickAction at
        # #/messages/<id> without a response_variable round-trip. Not
        # trusted as-is: automation_policy.py only checks this is present
        # and a string (a save-time value is typically still an unrendered
        # Jinja template, e.g. "{{ everrise_snap_ts }}_0"), so the real
        # check is _sanitize_notification_id below, against the RENDERED
        # value this handler actually receives at call time. Optional
        # because a hand-written automation calling this service directly
        # has no reason to care what id it gets — the response value below
        # covers that case.
        vol.Optional("notification_id"): cv.string,
    }
)

# What a notification_id is allowed to look like once rendered — this ends
# up part of a filesystem path (image_path below), so this is the actual
# security boundary for it, not automation_policy.py's much looser
# structural check (see LOG_NOTIFICATION_SCHEMA's comment on why that check
# can't do more than confirm a string is present).
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _sanitize_notification_id(raw: Any) -> str | None:
    if isinstance(raw, str) and _SAFE_ID_PATTERN.match(raw):
        return raw
    return None


def notifications_dir(hass: HomeAssistant) -> Path:
    return base_dir(hass) / NOTIFICATIONS_SUBDIR


def index_path(hass: HomeAssistant) -> Path:
    return notifications_dir(hass) / "index.json"


def images_dir(hass: HomeAssistant) -> Path:
    return notifications_dir(hass) / "images"


def image_path(hass: HomeAssistant, notification_id: str) -> Path:
    return images_dir(hass) / f"{notification_id}.jpg"


def load_index(path: Path) -> list[dict[str, Any]]:
    """The whole notification list. A missing or empty file is an empty
    list, not an error — see the module docstring: nothing writes here
    until the first `log_notification` call."""
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.error("Failed reading %s, treating it as empty: %s", path, err)
        return []
    if not isinstance(loaded, list):
        _LOGGER.error("%s does not contain a list, treating it as empty", path)
        return []
    return [item for item in loaded if isinstance(item, dict)]


def write_index_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def add_notification(
    hass: HomeAssistant,
    notification_id: str,
    title: str,
    message: str,
    target: str | None,
    has_image: bool,
) -> dict[str, Any]:
    """Runs on the executor (called via async_add_executor_job) — do the
    read, prepend, write here as one synchronous unit so two notifications
    logged in close succession can't race and clobber each other's write."""
    path = index_path(hass)
    records = load_index(path)
    record: dict[str, Any] = {
        "id": notification_id,
        "created": time.time(),
        "title": title,
        "message": message,
        "target": target,
        "has_image": has_image,
    }
    # Newest first, same convention the frontend reads it in — see
    # notifications_http.py's list view, which returns this order as-is.
    records = [record, *records]
    write_index_atomic(path, records)
    return record


def list_notifications(hass: HomeAssistant, limit: int = 200) -> list[dict[str, Any]]:
    records = load_index(index_path(hass))
    records.sort(key=lambda r: r.get("created", 0), reverse=True)
    return records[: max(0, limit)]


def get_notification(hass: HomeAssistant, notification_id: str) -> dict[str, Any] | None:
    for record in load_index(index_path(hass)):
        if record.get("id") == notification_id:
            return record
    return None


def prune_older_than(hass: HomeAssistant, days: int) -> int:
    """Deletes records (and their snapshot files, if any) older than
    `days`. Runs on the executor. Returns how many were removed, so the
    caller can decide whether it's worth logging."""
    path = index_path(hass)
    records = load_index(path)
    cutoff = time.time() - days * 86400
    keep: list[dict[str, Any]] = []
    removed = 0
    for record in records:
        if record.get("created", 0) >= cutoff:
            keep.append(record)
            continue
        removed += 1
        if record.get("has_image") and isinstance(record.get("id"), str):
            try:
                image_path(hass, record["id"]).unlink(missing_ok=True)
            except OSError as err:
                _LOGGER.warning("Couldn't remove snapshot for expired notification %s: %s", record.get("id"), err)
    if removed:
        write_index_atomic(path, keep)
    return removed


async def async_prune_notifications(hass: HomeAssistant, days: int) -> int:
    return await hass.async_add_executor_job(prune_older_than, hass, days)


async def async_handle_log_notification(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Handler for `everrise_dashboard.log_notification` — see
    LOG_NOTIFICATION_SCHEMA for the accepted fields. Returns {"id": ...} as
    a response variable regardless of whether the caller supplied its own
    id or got one generated here, so either way of calling this service can
    read back what was actually used."""
    title = call.data["title"]
    message = call.data["message"]
    target = call.data.get("target")
    camera_entity_id = call.data.get("camera_entity_id")

    notification_id = _sanitize_notification_id(call.data.get("notification_id"))
    if notification_id is None:
        if call.data.get("notification_id") is not None:
            _LOGGER.warning(
                "Ignoring an unsafe notification_id (%r) — generating one instead",
                call.data.get("notification_id"),
            )
        notification_id = uuid4().hex
    has_image = False

    if camera_entity_id:
        dest = image_path(hass, notification_id)
        await hass.async_add_executor_job(lambda: dest.parent.mkdir(parents=True, exist_ok=True))
        try:
            await hass.services.async_call(
                "camera",
                "snapshot",
                {"entity_id": camera_entity_id, "filename": str(dest)},
                blocking=True,
            )
            has_image = await hass.async_add_executor_job(dest.exists)
        except Exception as err:  # noqa: BLE001 - a failed snapshot shouldn't block logging the message itself
            _LOGGER.warning(
                "Couldn't capture a snapshot from %s for a logged notification: %s", camera_entity_id, err
            )

    record = await hass.async_add_executor_job(
        add_notification, hass, notification_id, title, message, target, has_image
    )
    return {"id": record["id"]}
