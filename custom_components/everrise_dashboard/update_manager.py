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

from .const import BRIDGE_RELEASES_LATEST_URL, DIST_RELEASES_LATEST_URL, DOMAIN
from .frontend_updater import get_installed_version
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
