"""Live "check for updates" logic behind the EverRise dashboard's own
Updates screen (update_http.py) — distinct from update.py's
_LatestVersionCoordinator, which polls in the background every 15 minutes
purely to drive HA's native Updates page. This module only runs when a
customer explicitly clicks "Check for updates", and never caches: every
call is a fresh GitHub hit for both repos.

Deliberately all-or-nothing: if either GitHub call fails, the whole check
fails and says which side broke, rather than returning whichever side did
succeed. A partially-known result (e.g. "no idea about the backend, but
here's the frontend") is a worse outcome here than a plain retry prompt —
this flow is what gates a real install, not just a status badge, so it
should never let a customer act on half a picture.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .bridge_updater import install_bridge_version
from .const import BRIDGE_RELEASES_LATEST_URL, DIST_RELEASES_LATEST_URL, DOMAIN
from .frontend_updater import get_installed_version, install_latest as install_frontend_version
from .update_consent import get_all_consents
from .version_compare import latest_is_newer

_LOGGER = logging.getLogger(__name__)

_RELEASE_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def _get_installed_backend_version(hass: HomeAssistant) -> str | None:
    """Whatever's actually running right now — read via Home Assistant's
    own integration loader rather than re-parsing manifest.json by hand, so
    this can never disagree with what HA itself thinks is installed."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except IntegrationNotFound:
        # Shouldn't happen — this code only ever runs FROM inside that same
        # integration — but fail safe into "unknown" rather than raise out
        # of the HTTP view if it somehow does.
        _LOGGER.error("Couldn't resolve this integration's own version via async_get_integration")
        return None
    version = integration.version
    return str(version) if version is not None else None


async def _fetch_latest_release(hass: HomeAssistant, url: str) -> tuple[str | None, str | None, str | None]:
    """One GitHub "latest release" call. Returns
    (version, release_notes, error) — exactly one of (version + notes) or
    error is meaningfully set. The failure reason is handed back rather
    than swallowed, matching frontend_updater.fetch_latest_version's
    existing discipline (see that function's docstring — a box that
    couldn't reach GitHub has silently reported a false "up to date"
    before; this flow refuses to repeat that, see check_for_updates
    below)."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url,
            timeout=_RELEASE_FETCH_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"},
        ) as resp:
            if resp.status != 200:
                return None, None, f"GitHub answered HTTP {resp.status}"
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        if isinstance(err, asyncio.TimeoutError):
            reason = f"Timed out after {int(_RELEASE_FETCH_TIMEOUT.total or 0)}s waiting for GitHub"
        else:
            reason = str(err) or err.__class__.__name__
        return None, None, reason

    tag = data.get("tag_name") if isinstance(data, dict) else None
    notes = data.get("body") if isinstance(data, dict) else None
    if not tag:
        return None, None, "GitHub's response had no tag_name"
    return str(tag), notes or "", None


async def check_for_updates(hass: HomeAssistant) -> dict[str, Any]:
    """The live check behind the Updates screen's "Check for updates"
    button. Always hits GitHub fresh — no caching, matching the "only
    checks when a customer actually asks" design.

    Returns either
        {"success": True, "backend": {...}, "frontend": {...}}
    or
        {"success": False, "error": "..."}
    — see the module docstring for why a partial result is never returned.
    Each side's dict (on success) is
        {"installed": str | None, "latest": str, "available": bool, "releaseNotes": str}
    """
    installed_backend, backend_release, installed_frontend, frontend_release = await asyncio.gather(
        _get_installed_backend_version(hass),
        _fetch_latest_release(hass, BRIDGE_RELEASES_LATEST_URL),
        get_installed_version(hass),
        _fetch_latest_release(hass, DIST_RELEASES_LATEST_URL),
    )
    latest_backend, backend_notes, backend_error = backend_release
    latest_frontend, frontend_notes, frontend_error = frontend_release

    errors = []
    if backend_error:
        errors.append(f"backend: {backend_error}")
    if frontend_error:
        errors.append(f"frontend: {frontend_error}")
    if errors:
        message = "Couldn't reach GitHub for the update check (" + "; ".join(errors) + "). Please try again."
        _LOGGER.warning("Update check failed: %s", message)
        return {"success": False, "error": message}

    return {
        "success": True,
        "backend": {
            "installed": installed_backend,
            "latest": latest_backend,
            "available": latest_is_newer(latest_backend, installed_backend),
            "releaseNotes": backend_notes,
        },
        "frontend": {
            "installed": installed_frontend,
            "latest": latest_frontend,
            "available": latest_is_newer(latest_frontend, installed_frontend),
            "releaseNotes": frontend_notes,
        },
    }

_RESTART_REQUIRED_ENTITY_ID = "binary_sensor.bridge_restart_required"


def _latest_consented_version(consents: list[dict[str, Any]], field: str) -> str | None:
    """Walks the consent history backward for the most recent entry that
    named a version for this side (backendVersion or frontendVersion) —
    consent.py's list is append-only and a single event can carry either
    or both fields, so the most recent NON-NULL value for this field is
    what "the customer's current consent" means, not just the last entry
    overall."""
    for entry in reversed(consents):
        value = entry.get(field)
        if value is not None:
            return value
    return None


async def _nudge_restart_required_sensor(hass: HomeAssistant) -> None:
    """Best-effort only — the sensor polls itself every 5 minutes
    regardless (see binary_sensor.py's SCAN_INTERVAL), this just avoids a
    stale "no restart needed" badge for that window right after a backend
    install. Never lets a failure here fail the install itself."""
    try:
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": _RESTART_REQUIRED_ENTITY_ID}
        )
    except Exception as err:  # noqa: BLE001 - deliberately broad, see docstring
        _LOGGER.debug("Couldn't nudge %s after install: %s", _RESTART_REQUIRED_ENTITY_ID, err)


async def install_updates(hass: HomeAssistant) -> dict[str, Any]:
    """The live install behind the Updates screen's "Install" action.

    Re-runs check_for_updates fresh (same all-or-nothing GitHub rule as
    the check endpoint itself — refuses and asks for a retry rather than
    guessing if GitHub is unreachable right now), then for each side only
    installs if BOTH:
      - available is True (a newer release genuinely exists), AND
      - the most recent consent on record for that side names EXACTLY
        that latest version.

    The second condition is what stops a stale consent from authorizing
    an install it never actually covered — a customer who approved 0.20.0
    does not thereby approve 0.21.0 if that shipped afterward. In the
    normal flow (accept immediately followed by install) this always
    matches; it only matters for the edge case of a release landing in
    the gap between the two.

    Right before actually writing anything for a qualifying side, this
    also re-reads what's currently installed one more time and compares it
    to the version about to be installed. Installing can take a while (a
    real download and extract), this endpoint has no admin gate, and a
    second install request arriving while the first is still running is a
    real scenario — so if the box is already on the target version by the
    time this gets here, it skips the redundant reinstall and reports
    alreadyUpToDate instead of silently doing the work twice.

    Returns
        {"success": False, "error": "..."}
    if the live GitHub check itself failed, or otherwise
        {
            "success": True,
            "backend": {
                "attempted": bool, "installed": bool, "version": str | None,
                "error": str | None, "alreadyUpToDate": bool,
            },
            "frontend": {
                "attempted": bool, "installed": bool, "version": str | None,
                "error": str | None, "alreadyUpToDate": bool,
            },
            "restartRequired": bool,
        }
    "attempted" is False either when that side had nothing consented-and-
    available to install, or when alreadyUpToDate caught it just before
    installing (neither is an error) — see each side's "error" for why
    "installed" is False despite "attempted" being True.
    """
    check = await check_for_updates(hass)
    if not check.get("success"):
        return check

    consents = await hass.async_add_executor_job(get_all_consents, hass)
    latest_backend_consent = _latest_consented_version(consents, "backendVersion")
    latest_frontend_consent = _latest_consented_version(consents, "frontendVersion")

    backend = check["backend"]
    frontend = check["frontend"]
    backend_qualifies = backend["available"] and latest_backend_consent == backend["latest"]
    frontend_qualifies = frontend["available"] and latest_frontend_consent == frontend["latest"]

    backend_result = {
        "attempted": False,
        "installed": False,
        "version": None,
        "error": None,
        "alreadyUpToDate": False,
    }
    frontend_result = {
        "attempted": False,
        "installed": False,
        "version": None,
        "error": None,
        "alreadyUpToDate": False,
    }

    if backend_qualifies:
        # One more, fresh, right-before-writing-anything check: the
        # "available" flag above came from check_for_updates() at the top
        # of this same call, but installing can take a while (a real
        # download+extract), and this endpoint has no admin gate — two
        # install requests in close succession are a real scenario, not a
        # hypothetical one. If the box is already on this exact version by
        # the time we actually get here, skip a redundant reinstall and
        # say so plainly rather than silently doing the work twice.
        current_backend_version = await _get_installed_backend_version(hass)
        if current_backend_version == backend["latest"]:
            backend_result["version"] = backend["latest"]
            backend_result["alreadyUpToDate"] = True
        else:
            backend_result["attempted"] = True
            backend_result["version"] = backend["latest"]
            ok = await install_bridge_version(hass, backend["latest"])
            backend_result["installed"] = ok
            if not ok:
                backend_result["error"] = "Backend install failed — check the Home Assistant log for details."

    if frontend_qualifies:
        # Same defensive re-check as the backend side just above.
        current_frontend_version = await get_installed_version(hass)
        if current_frontend_version == frontend["latest"]:
            frontend_result["version"] = frontend["latest"]
            frontend_result["alreadyUpToDate"] = True
        else:
            frontend_result["attempted"] = True
            frontend_result["version"] = frontend["latest"]
            ok = await install_frontend_version(hass, version=frontend["latest"])
            frontend_result["installed"] = ok
            if not ok:
                frontend_result["error"] = "Frontend install failed — check the Home Assistant log for details."

    if backend_result["installed"]:
        await _nudge_restart_required_sensor(hass)

    return {
        "success": True,
        "backend": backend_result,
        "frontend": frontend_result,
        "restartRequired": backend_result["installed"],
    }
