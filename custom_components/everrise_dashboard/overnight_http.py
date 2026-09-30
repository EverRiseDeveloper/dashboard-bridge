"""Authenticated HTTP API for the overnight summary — read-only.

Summaries are only ever written by overnight.py (at 6 am, or when
`everrise_dashboard.summarize_night` is called). Same non-admin pattern as
notifications_http.py: any logged-in household member may read their own
house's summary.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .overnight import read_nights

_LOGGER = logging.getLogger(__name__)

API_BASE = "/api/everrise_dashboard"


class EverriseOvernightView(HomeAssistantView):
    """GET the latest night's summary: {"latest": record | null}."""

    url = f"{API_BASE}/overnight"
    name = "api:everrise_dashboard:overnight"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        try:
            nights = await self._hass.async_add_executor_job(read_nights, self._hass)
        except (OSError, ValueError) as err:
            _LOGGER.error("Failed reading the overnight summary: %s", err)
            return self.json_message("Could not read the overnight summary.", HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json({"latest": nights[0] if nights else None})
