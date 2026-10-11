"""Authenticated HTTP API for the EverRise dashboard's own Updates screen.

Check, accept, install, and restart endpoints. Same HomeAssistantView
pattern as http.py/tailscale_http.py.
"""

from __future__ import annotations

import json
import logging
import time
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .update_consent import record_consent
from .update_history import ACTION_RESTART, record_update_event
from .update_manager import async_restart_pending, check_for_updates, install_updates
from .update_runtime import get_update_runtime

_LOGGER = logging.getLogger(__name__)

# A second "yes, restart" inside this window is the same restart, not a new
# one — Home Assistant takes a while to go down and come back.
_RESTART_DEDUPE_SECONDS = 180


class EverriseUpdateCheckView(HomeAssistantView):
    """GET-only — a live, uncached check against GitHub for both repos.

    Available to any logged-in user (read-only, same level as
    DashboardConfigView's GET).
    """

    url = "/api/everrise_dashboard/updates/check"
    name = "api:everrise_dashboard:updates:check"
    # requires_auth defaults to True on the base class — left unset deliberately.

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        result = await check_for_updates(self._hass)
        if not result.get("success"):
            # 502: this endpoint's own logic didn't fail — an upstream
            # (GitHub) call did. The error message already says which side.
            return self.json(result, status_code=HTTPStatus.BAD_GATEWAY)
        return self.json(result)


class EverriseUpdateAcceptView(HomeAssistantView):
    """POST — records that the logged-in user consents to installing the
    given version(s).

    Deliberately NO admin gate, unlike DashboardConfigView's writes: any
    authenticated household member can approve an update, not only
    whoever holds the HA admin account — see the project plan for why.
    Accountability comes from recording exactly who did it (hass_user's
    own id/name, taken from the authenticated session — never from
    anything the client sends, so it can't be spoofed as someone else),
    not from restricting who's allowed to.

    backendVersion and/or frontendVersion — whichever the customer ticked
    the checkbox for; either or both. This endpoint only RECORDS the
    decision — it doesn't touch GitHub and doesn't install anything.
    Installing is a separate, later endpoint, which re-validates the
    recorded consent still matches what's actually about to be installed
    before it does anything (that's where the real "don't act on stale
    consent" check lives, not here).
    """

    url = "/api/everrise_dashboard/updates/accept"
    name = "api:everrise_dashboard:updates:accept"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None:
            return self.json_message("Not authenticated.", HTTPStatus.UNAUTHORIZED)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self.json_message("Request body was not valid JSON.", HTTPStatus.BAD_REQUEST)
        if not isinstance(payload, dict):
            return self.json_message("Request body must be a JSON object.", HTTPStatus.BAD_REQUEST)

        backend_version = payload.get("backendVersion")
        frontend_version = payload.get("frontendVersion")
        if backend_version is None and frontend_version is None:
            return self.json_message(
                "Accept at least one of backendVersion or frontendVersion.", HTTPStatus.BAD_REQUEST
            )
        if backend_version is not None and not isinstance(backend_version, str):
            return self.json_message("backendVersion must be a string.", HTTPStatus.BAD_REQUEST)
        if frontend_version is not None and not isinstance(frontend_version, str):
            return self.json_message("frontendVersion must be a string.", HTTPStatus.BAD_REQUEST)

        entry = await self._hass.async_add_executor_job(
            lambda: record_consent(
                self._hass,
                user_id=hass_user.id,
                user_name=hass_user.name,
                backend_version=backend_version,
                frontend_version=frontend_version,
            )
        )
        return self.json({"success": True, "consent": entry})


class EverriseUpdateInstallView(HomeAssistantView):
    """POST — installs whichever side(s) currently qualify: a newer
    release genuinely available AND the most recent recorded consent for
    that side names exactly that version (see update_manager.install_updates
    for the full gating logic and why the second condition exists).

    Deliberately NO admin gate, same reasoning as EverriseUpdateAcceptView
    above — any authenticated household member can trigger an install of a
    version already consented to by someone in the household; this doesn't
    grant any new authority beyond what the accept endpoint already
    recorded.

    Re-checks GitHub live before installing anything (never trusts a
    cached/earlier check) and never installs a side that doesn't qualify —
    if neither side does, this returns a normal success response saying so,
    not an error.

    One install at a time per house: while one is running, a second request
    gets 409 with who started it (see update_manager.install_updates). Who
    tapped Install is recorded from the session, like accept's consent.
    """

    url = "/api/everrise_dashboard/updates/install"
    name = "api:everrise_dashboard:updates:install"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None:
            return self.json_message("Not authenticated.", HTTPStatus.UNAUTHORIZED)

        result = await install_updates(self._hass, user_id=hass_user.id, user_name=hass_user.name)
        if result.get("busy"):
            return self.json(result, status_code=HTTPStatus.CONFLICT)
        if not result.get("success"):
            # 502: same reasoning as the check endpoint — this view's own
            # logic didn't fail, the live GitHub re-check inside it did.
            return self.json(result, status_code=HTTPStatus.BAD_GATEWAY)
        return self.json(result)


class EverriseUpdateRestartView(HomeAssistantView):
    """POST — restarts Home Assistant to finish installing a bridge update,
    when the household member who installed it says yes. This is the only
    restart the update flow ever does: there is no scheduled one.

    Refused (409) while an install is running — a restart in the middle of
    swapping the bridge's files could leave it without its folder — and
    when there's no installed update waiting for a restart, so this can't
    be used to restart the house for any other reason. A second "yes"
    shortly after the first doesn't restart again. Who asked is recorded.

    Fire-and-forget: responds immediately and lets the restart itself happen
    in the background, since the HTTP response would never make it back to
    the client once the process is going down anyway.

    Deliberately NO admin gate, same reasoning as install/accept above.
    """

    url = "/api/everrise_dashboard/updates/restart"
    name = "api:everrise_dashboard:updates:restart"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None:
            return self.json_message("Not authenticated.", HTTPStatus.UNAUTHORIZED)

        runtime = get_update_runtime(self._hass)
        if runtime.install_lock.locked():
            who = (runtime.installing or {}).get("userName") or "Someone"
            return self.json(
                {
                    "success": False,
                    "busy": True,
                    "error": f"{who} is installing an update right now. Restart once it has finished.",
                    "installing": dict(runtime.installing or {}),
                },
                status_code=HTTPStatus.CONFLICT,
            )

        now = time.monotonic()
        if runtime.restart_requested_at is not None and now - runtime.restart_requested_at < _RESTART_DEDUPE_SECONDS:
            return self.json(
                {"success": True, "alreadyRestarting": True, "message": "Home Assistant is already restarting."}
            )

        if not await async_restart_pending(self._hass):
            return self.json(
                {"success": False, "error": "There's no installed update waiting for a restart."},
                status_code=HTTPStatus.CONFLICT,
            )

        runtime.restart_requested_at = now
        try:
            await self._hass.async_add_executor_job(
                lambda: record_update_event(
                    self._hass,
                    action=ACTION_RESTART,
                    user_id=hass_user.id,
                    user_name=hass_user.name,
                    result="requested",
                )
            )
        except Exception as err:  # noqa: BLE001 - never block the restart over the record
            _LOGGER.warning("Couldn't record who restarted for this update: %s", err)

        self._hass.async_create_task(self._hass.services.async_call("homeassistant", "restart"))
        return self.json({"success": True, "message": "Restarting Home Assistant now."})
