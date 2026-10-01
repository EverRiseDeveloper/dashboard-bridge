"""Authenticated HTTP API for the energy plan's advice — see plan_advice.py.

GET says where the advice stands. POST hands over the house's numbers, so
the home's AI is asked if it's due (at most every ten minutes for the whole
home), and answers once it has. Same non-admin pattern as overnight_http.py:
any logged-in household member may read it and ask for it.
"""

from __future__ import annotations

from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .plan_advice import advice_of
from .plan_advice_words import MAX_FACTS

API_BASE = "/api/everrise_dashboard"


class EverrisePlanAdviceView(HomeAssistantView):
    """GET {ai, advice, at, busy, updating, nextTry}; POST {"facts": text}
    for the same, after the AI has been asked if it was due."""

    url = f"{API_BASE}/plan_advice"
    name = "api:everrise_dashboard:plan_advice"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json(await advice_of(self._hass).view())

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Expected JSON.", HTTPStatus.BAD_REQUEST)
        facts = body.get("facts") if isinstance(body, dict) else None
        if not isinstance(facts, str) or not facts.strip() or len(facts) > MAX_FACTS:
            return self.json_message("Expected the house's numbers as text.", HTTPStatus.BAD_REQUEST)
        return self.json(await advice_of(self._hass).ask(facts))
