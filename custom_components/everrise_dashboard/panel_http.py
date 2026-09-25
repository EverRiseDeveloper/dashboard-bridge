"""The address Home Assistant loads the dashboard panel from.

The panel's code is one file with a fixed name — panel.js, served from
the static path in __init__.py — and that path sends no caching
instructions. Browsers are then free to keep their own copy for as long as
their heuristics like, and the Companion app's web view does exactly that:
confirmed live on 25 Sep 2026, when a box served the new 3.0.8 build but
an iPhone kept running 3.0.7 until the app's frontend cache was reset by
hand (sidebar → App configuration → Debugging → Reset frontend cache). The
dashboard's own version check (versionCheck.ts) noticed and reloaded the
page, but the reload asked for the same panel.js address and got the same
saved copy back.

So the panel is registered at this view instead, which answers every
request with a redirect to panel.js?v=<token>, where the token changes with
every build that lands in www/ (see frontend_updater's
_read_panel_cache_token). Each build is therefore at an address no browser
has saved yet, while an unchanged build keeps its address and stays
cached. The redirect itself is marked no-store so it is asked for fresh on
every load. It lives under /api/ because Home Assistant's own service
worker (Chrome, Android) always goes to the network for /api/ and
otherwise serves same-host files stale-while-revalidate. Nothing on this
page is private: the token is only a version number and a time, and the
same version is already public in version.json next to panel.js.

A module script follows the redirect and takes the final address as its
own, so panel.js's relative imports (./assets/…) still resolve inside the
static folder.
"""

from __future__ import annotations

from urllib.parse import quote

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .frontend_updater import get_panel_cache_token

# What the panel is registered under — see __init__.py's async_register_panel.
PANEL_ENTRY_URL = "/api/everrise_dashboard/panel.js"


class DashboardPanelEntryView(HomeAssistantView):
    """GET → 302 to the current build's panel.js address. No login needed:
    Home Assistant's frontend loads panel modules with a plain script tag,
    which carries no auth token."""

    url = PANEL_ENTRY_URL
    name = "api:everrise_dashboard:panel_entry"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, panel_js_url: str) -> None:
        self._hass = hass
        self._panel_js_url = panel_js_url

    async def get(self, request: web.Request) -> web.Response:
        token = await get_panel_cache_token(self._hass)
        # No panel.js yet (a brand-new box before its first install finishes):
        # point at the plain address, which 404s exactly as it did before.
        location = f"{self._panel_js_url}?v={quote(token, safe='')}" if token else self._panel_js_url
        return web.Response(
            status=302,
            headers={"Location": location, "Cache-Control": "no-store"},
        )
