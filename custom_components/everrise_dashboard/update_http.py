"""Authenticated HTTP API for the EverRise dashboard's own Updates screen.

Check, accept, install, and restart endpoints. Same HomeAssistantView
pattern as http.py/tailscale_http.py.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .update_consent import record_consent
from .update_manager import check_for_updates, install_updates

_LOGGER = logging.getLogger(__name__)


class EverriseUpdateCheckView(HomeAssistantView):
    """GET-only — a live, uncached check against GitHub for both repos.

    Available to any logged-in user (read-only, same level as
    DashboardConfigView's GET) — accepting/installing an update is a
    separate, admin-only endpoint added in a later phase.
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
    """

    url = "/api/everrise_dashboard/updates/install"
    name = "api:everrise_dashboard:updates:install"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None:
            return self.json_message("Not authenticated.", HTTPStatus.UNAUTHORIZED)

        result = await install_updates(self._hass)
        if not result.get("success"):
            # 502: same reasoning as the check endpoint — this view's own
            # logic didn't fail, the live GitHub re-check inside it did.
            return self.json(result, status_code=HTTPStatus.BAD_GATEWAY)
        return self.json(result)


class EverriseUpdateRestartView(HomeAssistantView):
    """POST — restarts Home Assistant to finish an install that flagged
    restartRequired. Fire-and-forget: responds immediately and lets the
    restart itself happen in the background, since the HTTP response would
    never actually make it back to the client once the process is going
    down anyway.

    Deliberately NO admin gate, same reasoning as install/accept above —
    finishing an update any household member already consented to and
    installed doesn't need a higher bar than starting it did.
    """

    url = "/api/everrise_dashboard/updates/restart"
    name = "api:everrise_dashboard:updates:restart"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None:
            return self.json_message("Not authenticated.", HTTPStatus.UNAUTHORIZED)

        self._hass.async_create_task(self._hass.services.async_call("homeassistant", "restart"))
        return self.json({"success": True, "message": "Restarting Home Assistant now."})
