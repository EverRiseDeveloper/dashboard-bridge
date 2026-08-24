"""Authenticated HTTP API for reading/writing the Everrise Dashboard's
config.json, replacing the old unauthenticated static-file approach.

Deep validation of the dashboard's config shape (rooms, cameras, header
stats, etc.) deliberately does NOT live here — that schema is defined and
kept up to date on the frontend (a zod schema mirroring the TypeScript
types in the dashboard repo), and the frontend validates before ever PUTing
here. This view only does structural sanity checks plus a safe write.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_FILENAME, CONF_FOLDER, DEFAULT_FILENAME, DEFAULT_FOLDER, DOMAIN
from .storage import read_json, resolve_config_path, write_json_atomic

_LOGGER = logging.getLogger(__name__)


class DashboardConfigView(HomeAssistantView):
    """GET/PUT the dashboard's config.json.

    GET is available to any logged-in user (the dashboard itself needs it
    to boot). PUT/POST additionally require an admin user, since this
    endpoint can rewrite every entity reference used across the house.
    """

    url = "/api/everrise_dashboard/config"
    name = "api:everrise_dashboard:config"
    # requires_auth defaults to True on the base class — left unset deliberately.

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _entry_options(self) -> tuple[str, str] | None:
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return None
        entry = entries[0]
        folder = entry.options.get(CONF_FOLDER, DEFAULT_FOLDER)
        filename = entry.options.get(CONF_FILENAME, DEFAULT_FILENAME)
        return folder, filename

    async def get(self, request: web.Request) -> web.Response:
        options = self._entry_options()
        if options is None:
            return self.json_message(
                "Everrise Dashboard Config Bridge is installed but not configured.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        folder, filename = options
        path = resolve_config_path(self._hass, folder, filename)
        if path is None:
            return self.json_message("Invalid folder/filename configuration.", HTTPStatus.BAD_REQUEST)
        if not path.exists():
            return self.json_message(f"No config saved yet at {path}.", HTTPStatus.NOT_FOUND)

        try:
            data = await self._hass.async_add_executor_job(read_json, path)
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Failed reading %s: %s", path, err)
            return self.json_message("Could not read the saved config file.", HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.json(data)

    async def _write(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None or not hass_user.is_admin:
            return self.json_message(
                "Only an admin user can edit the dashboard config.", HTTPStatus.FORBIDDEN
            )

        options = self._entry_options()
        if options is None:
            return self.json_message(
                "Everrise Dashboard Config Bridge is installed but not configured.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        folder, filename = options
        path = resolve_config_path(self._hass, folder, filename)
        if path is None:
            return self.json_message("Invalid folder/filename configuration.", HTTPStatus.BAD_REQUEST)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self.json_message("Request body was not valid JSON.", HTTPStatus.BAD_REQUEST)

        if not isinstance(payload, dict) or "household" not in payload or "headerStats" not in payload:
            return self.json_message(
                'Config must be a JSON object with at least "household" and "headerStats" keys.',
                HTTPStatus.BAD_REQUEST,
            )

        try:
            await self._hass.async_add_executor_job(write_json_atomic, path, payload)
        except OSError as err:
            _LOGGER.error("Failed writing %s: %s", path, err)
            return self.json_message("Could not save the config file.", HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.json(payload)

    async def put(self, request: web.Request) -> web.Response:
        return await self._write(request)

    async def post(self, request: web.Request) -> web.Response:
        return await self._write(request)
