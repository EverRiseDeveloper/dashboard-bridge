"""Authenticated HTTP API for Home's AI headlines — see headlines.py.

GET says where the headlines stand. POST asks the home's AI for new ones if
they're due (at most once an hour for the whole home) and answers once it
has. The dashboard only POSTs while Home is on screen and the switch in
Admin -> Home is on. Same non-admin pattern as plan_advice_http.py: any
logged-in household member may read them and ask for them.
"""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .headlines import headlines_of

API_BASE = "/api/everrise_dashboard"


class EverriseHeadlinesView(HomeAssistantView):
    """GET {ai, lines, at, model, busy, updating, nextTry}; POST (no body)
    for the same, after the AI has been asked if it was due."""

    url = f"{API_BASE}/headlines"
    name = "api:everrise_dashboard:headlines"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json(await headlines_of(self._hass).view())

    async def post(self, request: web.Request) -> web.Response:
        return self.json(await headlines_of(self._hass).ask())
