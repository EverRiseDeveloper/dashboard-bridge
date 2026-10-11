"""Check and install logic behind the EverRise dashboard's own Software
update screen (update_http.py), also used by the read-only background check
(update_notifier.py). There is no other way an update gets installed: the
old update entity and every auto-install automation are gone.

The check is deliberately all-or-nothing: if either GitHub call fails, the
whole check fails and says which side broke, rather than returning
whichever side did succeed. A partially-known result (e.g. "no idea about
the backend, but here's the frontend") is a worse outcome here than a plain
retry prompt — this flow is what gates a real install, not just a status
badge, so it should never let a customer act on half a picture.

Installs run one at a time per house (see update_runtime.py): a second tap
while one is running is turned away with who started it, rather than
downloading and swapping the same files twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from .bridge_updater import install_bridge_version
from .const import BRIDGE_RELEASES_LATEST_URL, DIST_RELEASES_LATEST_URL, DOMAIN
from .frontend_updater import get_installed_version, install_frontend_version
from .update_consent import get_all_consents
from .update_history import ACTION_INSTALL, record_update_event
from .update_runtime import UpdateRuntime, get_update_runtime
from .version_compare import latest_is_newer

_LOGGER = logging.getLogger(__name__)

_RELEASE_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)


# This integration's own manifest.json, read fresh off disk. Once a bridge
# update has been installed it holds the NEW version, even though the old
# code keeps running until Home Assistant restarts.
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


def _read_manifest_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version is not None else None


async def _get_loaded_backend_version(hass: HomeAssistant) -> str | None:
    """The version this running process actually loaded at startup — Home
    Assistant's integration loader caches the manifest it imported, so
    this does NOT change when new files land on disk."""
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


async def _get_installed_backend_version(hass: HomeAssistant) -> str | None:
    """What's installed on disk, which is what "is there anything newer?"
    has to compare against. Using the loaded version instead meant a
    customer who installed a bridge update and chose "Not now" for the
    restart was offered — and could reinstall — the same update again,
    because the running code still reported the old version."""
    on_disk = await hass.async_add_executor_job(_read_manifest_version, _MANIFEST_PATH)
    if on_disk is not None:
        return on_disk
    return await _get_loaded_backend_version(hass)


async def async_restart_pending(hass: HomeAssistant) -> bool:
    """True when a bridge update is installed on disk but the running code
    is still the old one — the only time the Software update screen's
    restart is allowed (see update_http.py)."""
    on_disk = await hass.async_add_executor_job(_read_manifest_version, _MANIFEST_PATH)
    loaded = await _get_loaded_backend_version(hass)
    return on_disk is not None and loaded is not None and on_disk != loaded


async def _fetch_latest_release(hass: HomeAssistant, url: str) -> tuple[str | None, str | None, str | None]:
    """One GitHub "latest release" call. Returns
    (version, release_notes, error) — exactly one of (version + notes) or
    error is meaningfully set. The failure reason is handed back rather
    than swallowed: a box that couldn't reach GitHub has silently reported
    a false "up to date" before (the old update entity did exactly that on
    Client_003), and this flow refuses to repeat it — see check_for_updates
    below."""
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
    """The live check behind the Software update screen and the background
    check. Always hits GitHub fresh — never a cached answer. Read-only:
    nothing here downloads or installs anything.

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

async def fetch_latest_frontend_release(hass: HomeAssistant) -> tuple[str | None, str | None]:
    """The newest tagged dashboard-dist release, as (version, error) — for
    the first-run bootstrap, which installs that exact release rather than
    whatever is on the main branch."""
    version, _notes, error = await _fetch_latest_release(hass, DIST_RELEASES_LATEST_URL)
    return version, error


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


async def install_updates(hass: HomeAssistant, *, user_id: str | None, user_name: str | None) -> dict[str, Any]:
    """The install behind the Software update screen's Install button — the
    only way an update gets installed.

    One install at a time per house. If one is already running, this
    returns straight away with
        {"success": False, "busy": True, "error": "...", "installing": {...}}
    (who started it and when) and touches nothing. Otherwise it holds the
    lock for the whole install, records who tapped Install and what
    happened (update_history.py), and returns what _install_qualifying
    returns.
    """
    runtime = get_update_runtime(hass)
    # No await between this check and taking the lock, so two taps can't
    # both get past it.
    if runtime.install_lock.locked():
        installing = dict(runtime.installing or {})
        who = installing.get("userName") or "Someone"
        return {
            "success": False,
            "busy": True,
            "error": f"{who} is already installing this update. It will be ready in a moment.",
            "installing": installing,
        }

    async with runtime.install_lock:
        runtime.installing = {
            "userId": user_id,
            "userName": user_name,
            "startedAt": dt_util.now().isoformat(),
            "backendVersion": None,
            "frontendVersion": None,
        }
        runtime.async_notify()
        result: dict[str, Any] | None = None
        try:
            result = await _install_qualifying(hass, runtime)
            return result
        finally:
            runtime.installing = None
            runtime.async_notify()
            await _record_install(hass, user_id, user_name, result)


async def _record_install(
    hass: HomeAssistant, user_id: str | None, user_name: str | None, result: dict[str, Any] | None
) -> None:
    """Never lets a failure to write the record fail the install itself."""
    if result is None:
        outcome, backend_version, frontend_version, detail = "error", None, None, "Install stopped unexpectedly"
    elif not result.get("success"):
        outcome, backend_version, frontend_version, detail = "check_failed", None, None, result.get("error")
    else:
        sides = [result["backend"], result["frontend"]]
        attempted = [side for side in sides if side["attempted"]]
        if not attempted:
            outcome = "nothing_to_install"
        elif all(side["installed"] for side in attempted):
            outcome = "installed"
        elif any(side["installed"] for side in attempted):
            outcome = "partly_installed"
        else:
            outcome = "failed"
        backend_version = result["backend"]["version"] if result["backend"]["attempted"] else None
        frontend_version = result["frontend"]["version"] if result["frontend"]["attempted"] else None
        detail = "; ".join(side["error"] for side in sides if side["error"]) or None
    try:
        await hass.async_add_executor_job(
            lambda: record_update_event(
                hass,
                action=ACTION_INSTALL,
                user_id=user_id,
                user_name=user_name,
                result=outcome,
                backend_version=backend_version,
                frontend_version=frontend_version,
                detail=detail,
            )
        )
    except Exception as err:  # noqa: BLE001 - see docstring
        _LOGGER.warning("Couldn't record who installed this update: %s", err)


async def _install_qualifying(hass: HomeAssistant, runtime: UpdateRuntime) -> dict[str, Any]:
    """Runs only while holding the install lock (see install_updates).

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
    to the version about to be installed — if the box is already on it,
    this skips the reinstall and reports alreadyUpToDate.

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

    # Let every open Software update screen say what's being installed.
    if runtime.installing is not None:
        runtime.installing["backendVersion"] = backend["latest"] if backend_qualifies else None
        runtime.installing["frontendVersion"] = frontend["latest"] if frontend_qualifies else None
        runtime.async_notify()

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
        # One more, fresh, right-before-writing-anything check. The install
        # lock already stops two installs overlapping; this still catches
        # a box that's already on this exact version (say, installed a
        # moment ago by someone else), and says so rather than reinstalling.
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
        runtime.async_mark_installed("backend", backend["latest"])
    if frontend_result["installed"]:
        runtime.async_mark_installed("frontend", frontend["latest"])

    return {
        "success": True,
        "backend": backend_result,
        "frontend": frontend_result,
        "restartRequired": backend_result["installed"],
    }
