"""Authenticated HTTP API backing the dashboard's Tailscale onboarding
banner (see TailscaleConnectBanner.tsx in the dashboard repo) — a login
URL to open, and a way to ask "did that actually work" without either the
dashboard or this bridge ever holding a Tailscale credential of its own.
Both views are best-effort reads of the add-on's own log via
tailscale_supervisor.py; see that module for the heuristic and its
caveats.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .tailscale_supervisor import (
    async_find_tailscale_addon_slug,
    async_is_tailscale_authenticated,
    async_trigger_fresh_login_url,
)

_LOGGER = logging.getLogger(__name__)


async def _async_find_slug_or_none(hass: HomeAssistant) -> str | None:
    slug = await async_find_tailscale_addon_slug(hass)
    if slug is None:
        _LOGGER.debug(
            "No Tailscale add-on found via Supervisor — either this isn't a "
            "Supervisor-managed install, or the add-on isn't installed on this box."
        )
    return slug


class TailscaleLoginUrlView(HomeAssistantView):
    """GET a fresh Tailscale login URL, for the dashboard's Authenticate
    button to open in the customer's own system browser (see that
    component's comment on why it must NOT be embedded/iframed — Google
    and Microsoft both refuse to complete a sign-in inside an embedded
    webview).

    Actively triggers a new login attempt rather than just reading
    whatever happens to already be in the log — see
    async_trigger_fresh_login_url's docstring: the add-on's own startup
    sequence is what generates this URL (no button click needed on the
    add-on's own page at all, which is good, since that page has a known
    bug where its own "Log In" button crashes trying to auto-open the
    URL — see https://github.com/hassio-addons/app-tailscale/issues/52),
    so this view forces that by restarting the add-on via Supervisor,
    then polls the log for the fresh URL. That means this call can take
    up to several seconds — the dashboard's banner shows a message
    explaining the wait rather than a bare spinner.

    Deliberately a JSON response the frontend opens itself
    (window.open(url, '_blank')), not a server-side redirect — a redirect
    would have to be hit as a plain, unauthenticated browser navigation,
    which breaks the moment this view requires a logged-in session the
    same way every other endpoint in this bridge does.
    """

    url = "/api/everrise_dashboard/tailscale/login_url"
    name = "api:everrise_dashboard:tailscale:login_url"
    # requires_auth defaults to True on the base class — left unset
    # deliberately, same convention as http.py's DashboardConfigView.

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        slug = await _async_find_slug_or_none(self._hass)
        if slug is None:
            return self.json_message(
                "Tailscale add-on not found on this box.", HTTPStatus.SERVICE_UNAVAILABLE
            )

        # Skip the restart entirely if this device is already connected —
        # both to avoid pointlessly bouncing a working connection, and
        # because a restart wouldn't produce a login URL at all in that
        # case (see async_is_tailscale_authenticated).
        authenticated = await async_is_tailscale_authenticated(self._hass, slug)
        if authenticated:
            return self.json_message(
                "Tailscale is already connected on this device — nothing to authenticate.",
                HTTPStatus.CONFLICT,
            )

        url = await async_trigger_fresh_login_url(self._hass, slug)
        if url is None:
            return self.json_message(
                "Couldn't get a fresh Tailscale login link — try again in a moment.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return self.json({"url": url})


class TailscaleStatusView(HomeAssistantView):
    """GET whether Tailscale is authenticated, straight from the add-on's
    own log — no stored state anywhere.

    The dashboard calls this both on mount (to decide whether to show the
    banner at all) and again after the customer taps "Done". There's
    nothing to flip or persist: the log itself is the only source of
    truth, so this just asks it fresh every time rather than caching the
    answer in a helper entity that could drift from what the add-on
    actually reports. Returns {"authenticated": false} (200, not an
    error) for the ordinary "not yet, keep waiting" case — the banner
    handles that by asking the customer to try again in a moment, not by
    treating it as a failure.
    """

    url = "/api/everrise_dashboard/tailscale/status"
    name = "api:everrise_dashboard:tailscale:status"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        slug = await _async_find_slug_or_none(self._hass)
        if slug is None:
            return self.json_message(
                "Tailscale add-on not found on this box.", HTTPStatus.SERVICE_UNAVAILABLE
            )

        authenticated = await async_is_tailscale_authenticated(self._hass, slug)
        if authenticated is None:
            return self.json_message(
                "Couldn't check the Tailscale add-on's status just now — try again in a moment.",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return self.json({"authenticated": authenticated})
