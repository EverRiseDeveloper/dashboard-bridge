"""Durable, append-only record of who consented to installing an EverRise
dashboard update, and when — the audit trail behind update_http.py's
accept endpoint. Lives in the same per-client storage root as config.json
(see storage.py), as update_consent.json.

Deliberately a growing list, not a single overwritten record: any
logged-in household member can consent (see update_http.py — no admin
gate on that endpoint, by design), so "who approved this" only means
something if every past consent is kept, not just the most recent one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .storage import base_dir, read_json, write_json_atomic

_CONSENT_FILENAME = "update_consent.json"


def _consent_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / _CONSENT_FILENAME


def _read_all(hass: HomeAssistant) -> list[dict[str, Any]]:
    path = _consent_path(hass)
    if not path.exists():
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError):
        # A corrupt/unreadable file shouldn't block recording a NEW
        # consent — starting a fresh list is safer than raising into the
        # HTTP view over history we can't make sense of anyway.
        return []
    return data if isinstance(data, list) else []


def record_consent(
    hass: HomeAssistant,
    *,
    user_id: str,
    user_name: str | None,
    backend_version: str | None,
    frontend_version: str | None,
) -> dict[str, Any]:
    """Appends one consent event (blocking file I/O — call via
    hass.async_add_executor_job, see update_http.py) and returns it.
    Callers are expected to have already validated that at least one of
    backend_version/frontend_version is set."""
    entry = {
        "acceptedAt": dt_util.utcnow().isoformat(),
        "userId": user_id,
        "userName": user_name,
        "backendVersion": backend_version,
        "frontendVersion": frontend_version,
    }
    path = _consent_path(hass)
    existing = _read_all(hass)
    existing.append(entry)
    write_json_atomic(path, existing)
    return entry


def get_all_consents(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Not used by any endpoint yet — kept available for a future "consent
    history" view (Admin/About tab) without needing a new storage format
    later."""
    return _read_all(hass)
