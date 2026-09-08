"""Automation create/read/update/delete for NON-ADMIN dashboard users.

Home Assistant's own `/api/config/automation/config/*` endpoints require an
admin account and there is no user policy that grants a non-admin access to
them. Every EverRise client is non-admin, so the dashboard's automation
builder could not save anything for the people it was built for; the
failures surfaced in the HA log as repeated "request with invalid
authentication" entries against that URL, which `http.ban` additionally
counts as failed login attempts.

These views close that gap. They require a logged-in user but NOT an admin
one, and perform the write in-process, where this integration already has
the privileges HA denies the caller. Every payload is checked against
automation_policy.validate_client_automation first — see that module for why
an allowlist rather than a passthrough is the whole point.

Contrast with DashboardConfigView, which keeps its admin gate on writes:
config.json defines every entity reference in the house and is the
installer's business, whereas an automation built from the curated rosters
is the household's own.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .automation_policy import is_client_owned_id, validate_client_automation
from .automations_store import (
    automations_path,
    load_automations,
    remove,
    upsert,
    write_automations_atomic,
)

_LOGGER = logging.getLogger(__name__)

API_BASE = "/api/everrise_dashboard"


async def _reload_automations(hass: HomeAssistant) -> None:
    """Called with no user context, so it runs with this integration's
    privileges rather than the caller's — which is the point, since
    automation.reload is not something a non-admin may invoke directly."""
    await hass.services.async_call("automation", "reload", blocking=True)


def _client_automations(automations: list[dict]) -> list[dict]:
    return [a for a in automations if is_client_owned_id(a.get("id"))]


class _AutomationViewBase(HomeAssistantView):
    # requires_auth defaults True on the base class. Deliberately NOT
    # requiring admin — that restriction is what this whole module exists
    # to work around, and the allowlist is what makes it safe.

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _load(self) -> list[dict]:
        path = automations_path(self._hass)
        return await self._hass.async_add_executor_job(load_automations, path)

    async def _save(self, automations: list[dict]) -> None:
        path = automations_path(self._hass)
        await self._hass.async_add_executor_job(write_automations_atomic, path, automations)
        await _reload_automations(self._hass)


class EverriseAutomationsView(_AutomationViewBase):
    """GET the full list of client-built automations, configs included.

    The dashboard needs the configs (not just the entity states) to render
    each automation's plain-English summary and to reopen one in the
    builder. Only client-owned ids are returned: the installer's own
    automations and this integration's seeded ones are none of the
    household's business, and leaking their contents to a non-admin user
    would be a small information disclosure for no benefit.
    """

    url = f"{API_BASE}/automations"
    name = "api:everrise_dashboard:automations"

    async def get(self, request: web.Request) -> web.Response:
        try:
            automations = await self._load()
        except (OSError, ValueError) as err:
            _LOGGER.error("Failed reading automations.yaml: %s", err)
            return self.json_message("Could not read automations.", HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json(_client_automations(automations))


class EverriseAutomationView(_AutomationViewBase):
    """GET / POST (upsert) / DELETE one automation by its config id."""

    url = f"{API_BASE}/automation/{{automation_id}}"
    name = "api:everrise_dashboard:automation"

    async def get(self, request: web.Request, automation_id: str) -> web.Response:
        if not is_client_owned_id(automation_id):
            return self.json_message("Not a dashboard-built automation.", HTTPStatus.FORBIDDEN)
        try:
            automations = await self._load()
        except (OSError, ValueError) as err:
            _LOGGER.error("Failed reading automations.yaml: %s", err)
            return self.json_message("Could not read automations.", HTTPStatus.INTERNAL_SERVER_ERROR)

        for automation in automations:
            if automation.get("id") == automation_id:
                return self.json(automation)
        return self.json_message("No such automation.", HTTPStatus.NOT_FOUND)

    async def post(self, request: web.Request, automation_id: str) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self.json_message("Request body was not valid JSON.", HTTPStatus.BAD_REQUEST)

        # The id in the URL wins, so a payload cannot be aimed at a
        # different automation than the one the caller addressed.
        if isinstance(payload, dict):
            payload = {**payload, "id": automation_id}

        problems = validate_client_automation(payload)
        if problems:
            user = request.get("hass_user")
            _LOGGER.warning(
                "Rejected an automation save from %s: %s",
                getattr(user, "name", "unknown user"),
                "; ".join(problems),
            )
            return self.json({"message": "Automation was rejected.", "errors": problems}, HTTPStatus.BAD_REQUEST)

        try:
            automations = await self._load()
            await self._save(upsert(automations, payload))
        except (OSError, ValueError) as err:
            _LOGGER.error("Failed writing automations.yaml: %s", err)
            return self.json_message("Could not save the automation.", HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.json(payload)

    async def delete(self, request: web.Request, automation_id: str) -> web.Response:
        if not is_client_owned_id(automation_id):
            return self.json_message(
                "Only automations built in the dashboard can be deleted here.", HTTPStatus.FORBIDDEN
            )
        try:
            automations = await self._load()
            kept, removed = remove(automations, automation_id)
            if not removed:
                return self.json_message("No such automation.", HTTPStatus.NOT_FOUND)
            await self._save(kept)
        except (OSError, ValueError) as err:
            _LOGGER.error("Failed writing automations.yaml: %s", err)
            return self.json_message("Could not delete the automation.", HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.json_message("Deleted.", HTTPStatus.OK)
