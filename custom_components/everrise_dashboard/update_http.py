"""Authenticated HTTP API for the EverRise dashboard's own Updates screen.

Only the "check for updates" endpoint so far — accept/install/restart are a
later phase (see the update-consent project plan). Same HomeAssistantView
pattern as http.py/tailscale_http.py.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .update_manager import check_for_updates

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
