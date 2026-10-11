"""Durable record of who installed or restarted for an EverRise update, and
what happened — the companion to update_consent.py's record of who agreed
to the T&C. Lives next to it in the per-client storage root as
update_history.json.

Every install tap and every restart request is kept, with the user taken
from the logged-in Home Assistant session (never from anything the
dashboard sends), so "who did this" can't be spoofed. Newest last. Kept to
the most recent _MAX_ENTRIES so the file can't grow without bound — years
of updates at any realistic release pace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .storage import base_dir, read_json, write_json_atomic

_HISTORY_FILENAME = "update_history.json"
_MAX_ENTRIES = 500

ACTION_INSTALL = "install"
ACTION_RESTART = "restart"
ACTION_FIRST_INSTALL = "first_install"


def _history_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / _HISTORY_FILENAME


def _read_all(hass: HomeAssistant) -> list[dict[str, Any]]:
    path = _history_path(hass)
    if not path.exists():
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError):
        # Unreadable history shouldn't stop recording what happens next.
        return []
    return data if isinstance(data, list) else []


def record_update_event(
    hass: HomeAssistant,
    *,
    action: str,
    user_id: str | None,
    user_name: str | None,
    result: str,
    backend_version: str | None = None,
    frontend_version: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Appends one event (blocking file I/O — call via
    hass.async_add_executor_job) and returns it."""
    entry: dict[str, Any] = {
        # HA's own local time with its offset, same as update_consent.py.
        "at": dt_util.now().isoformat(),
        "action": action,
        "userId": user_id,
        "userName": user_name,
        "backendVersion": backend_version,
        "frontendVersion": frontend_version,
        "result": result,
    }
    if detail:
        entry["detail"] = detail
    history = _read_all(hass)
    history.append(entry)
    write_json_atomic(_history_path(hass), history[-_MAX_ENTRIES:])
    return entry


def get_update_history(hass: HomeAssistant) -> list[dict[str, Any]]:
    return _read_all(hass)
