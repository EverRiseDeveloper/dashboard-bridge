"""Authenticated HTTP API for the dashboard's Message Centre — read-only.

Records themselves are only ever created through the
`everrise_dashboard.log_notification` service (see notifications_store.py);
there is no POST/PUT here, deliberately — this is a log an automation
writes to, not a document the dashboard edits.

Same non-admin-gated pattern as automations_http.py: any logged-in
household member may read their own house's notification history and
snapshots, not just an admin.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .notifications_store import get_notification, image_path, list_notifications

_LOGGER = logging.getLogger(__name__)

API_BASE = "/api/everrise_dashboard"

# Upper bound on the ?limit= query param — generous for any real household's
# notification volume, just a guard against an unbounded response.
MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 200


class EverriseNotificationsView(HomeAssistantView):
    """GET the notification list, newest first."""

    url = f"{API_BASE}/notifications"
    name = "api:everrise_dashboard:notifications"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", DEFAULT_LIST_LIMIT))
        except ValueError:
            return self.json_message("limit must be an integer.", HTTPStatus.BAD_REQUEST)
        limit = max(1, min(limit, MAX_LIST_LIMIT))

        try:
            records = await self._hass.async_add_executor_job(list_notifications, self._hass, limit)
        except OSError as err:
            _LOGGER.error("Failed reading notification history: %s", err)
            return self.json_message("Could not read notification history.", HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json(records)


class EverriseNotificationView(HomeAssistantView):
    """GET a single notification by id — what a push notification's
    clickAction deep-links to (#/messages/<id> in the dashboard)."""

    url = f"{API_BASE}/notifications/{{notification_id}}"
    name = "api:everrise_dashboard:notification"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, notification_id: str) -> web.Response:
        try:
            record = await self._hass.async_add_executor_job(get_notification, self._hass, notification_id)
        except OSError as err:
            _LOGGER.error("Failed reading notification %s: %s", notification_id, err)
            return self.json_message("Could not read this notification.", HTTPStatus.INTERNAL_SERVER_ERROR)
        if record is None:
            return self.json_message("No such notification.", HTTPStatus.NOT_FOUND)
        return self.json(record)


class EverriseNotificationImageView(HomeAssistantView):
    """GET the snapshot attached to a notification, if it has one.

    Streamed from bridge-managed storage rather than served from `www/` —
    unlike the existing snapshot-attach flow's public /local/ path, this
    requires the same login as everything else in the dashboard.
    """

    url = f"{API_BASE}/notifications/{{notification_id}}/image"
    name = "api:everrise_dashboard:notification_image"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, notification_id: str) -> web.StreamResponse:
        path = image_path(self._hass, notification_id)
        exists = await self._hass.async_add_executor_job(path.exists)
        if not exists:
            return self.json_message("No snapshot saved for this notification.", HTTPStatus.NOT_FOUND)
        return web.FileResponse(path)
