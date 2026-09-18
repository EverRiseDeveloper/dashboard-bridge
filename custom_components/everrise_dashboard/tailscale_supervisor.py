"""Helpers for reading the Tailscale add-on's state through Home Assistant
Supervisor's own internal API — and for handing the dashboard's onboarding
banner (see TailscaleConnectBanner.tsx in the dashboard repo, driven
through tailscale_http.py) a login URL and an authenticated/not verdict,
without either of them ever touching a customer's Tailscale account
directly.

Deliberately talks to Supervisor directly (http://supervisor, authenticated
with the SUPERVISOR_TOKEN env var already available inside this process)
rather than shelling out or depending on the add-on exposing anything of
its own — the add-on has no documented API or entity for its connection
state or its pending login URL, and Supervisor's `/addons/<slug>/logs/latest`
endpoint is the same data a person would otherwise have to open Settings >
Add-ons > Tailscale > Log and read by hand.

Only works on Supervisor-managed installs (Home Assistant OS / Supervised).
A plain Core-only or Container install has no SUPERVISOR_TOKEN and no
Supervisor to ask, so every function here degrades to returning None rather
than raising — callers treat that as "can't tell right now", not an error
worth surfacing loudly on every request.
"""

from __future__ import annotations

import logging
import os
import re

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_BASE_URL = "http://supervisor"
_REQUEST_TIMEOUT = ClientTimeout(total=10)

# Matches this add-on's slug regardless of which repository it was
# installed from — the human-readable name always ends in "tailscale", but
# the leading segment is a hash of the add-on repository's URL (confirmed
# against the provisioner's own catalog: "a0d7b954_tailscale" for the Home
# Assistant Community Add-ons repository), so it's looked up rather than
# hardcoded.
_TAILSCALE_SLUG_SUFFIX = "_tailscale"

# The URL this add-on reprints, once per pending login attempt, while the
# device is not yet authenticated to a tailnet — confirmed against a real
# device's log, which showed repeated blocks of
#     To authenticate, visit:
#         https://login.tailscale.com/a/<code>
# each followed shortly by a "tailscaleUp(reauth=true) ..." retry line, on
# a roughly 15-30 second cycle for as long as nobody completes that login.
# Each cycle prints a DIFFERENT code — the previous one expires — so where
# more than one match turns up in the tail, only the LAST one is current.
_LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+")

# Only the tail of the log matters — the full session log can span the
# add-on's entire uptime, and an old login attempt from hours or days ago
# (a customer who force-reauthed, or a first attempt nobody completed
# before it expired) shouldn't count against a device that authenticated
# successfully since. 200 lines comfortably covers several minutes of the
# reauth loop's own cadence with margin either side, without requiring any
# log timestamp parsing (whose timezone relative to this process isn't
# something to assume).
_LOG_TAIL_LINES = 200


def _auth_headers() -> dict[str, str] | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


async def async_find_tailscale_addon_slug(hass: HomeAssistant) -> str | None:
    """Look up the installed Tailscale add-on's slug via Supervisor's own
    add-on list, rather than hardcoding one — see _TAILSCALE_SLUG_SUFFIX's
    docstring for why. Returns None on anything short of a clean match: no
    Supervisor, no token, the add-on isn't installed, or the response
    didn't parse the way expected. Callers treat None as "can't check
    right now", not a fatal error — this is re-looked-up on every request
    rather than cached, so it stays correct across an add-on reinstall
    without needing this integration restarted.
    """
    headers = _auth_headers()
    if headers is None:
        return None

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{_SUPERVISOR_BASE_URL}/addons", headers=headers, timeout=_REQUEST_TIMEOUT
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Supervisor /addons returned HTTP %s", resp.status)
                return None
            payload = await resp.json()
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Couldn't reach Supervisor to list add-ons: %s", err)
        return None

    addons = payload.get("data", {}).get("addons", [])
    for addon in addons:
        slug = addon.get("slug", "")
        if slug.endswith(_TAILSCALE_SLUG_SUFFIX):
            return slug
    return None


async def _async_fetch_log_tail(hass: HomeAssistant, slug: str) -> str | None:
    """Shared by the two functions below — one Supervisor log fetch,
    trimmed to its last _LOG_TAIL_LINES lines. None on any failure (no
    Supervisor/token, add-on not running, network hiccup)."""
    headers = _auth_headers()
    if headers is None:
        return None

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{_SUPERVISOR_BASE_URL}/addons/{slug}/logs/latest",
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Supervisor logs/latest for %s returned HTTP %s", slug, resp.status)
                return None
            text = await resp.text()
    except (ClientError, TimeoutError) as err:
        _LOGGER.debug("Couldn't fetch %s's log from Supervisor: %s", slug, err)
        return None

    return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


async def async_is_tailscale_authenticated(hass: HomeAssistant, slug: str) -> bool | None:
    """Best-effort read of whether the Tailscale add-on is past its initial
    login (True), still waiting on it (False), or the state can't be
    determined right now (None — no Supervisor/token, add-on not running,
    network hiccup, etc).

    Heuristic, not an authoritative API — see _LOGIN_URL_RE's docstring:
    while a login is pending, the add-on reprints a fresh login URL on a
    tight retry cycle, so its absence from a reasonably-sized recent tail
    of the log is a good proxy for "that loop has stopped, the device got
    authenticated". This is exactly what the onboarding banner's "Done"
    button triggers (see tailscale_http.py's TailscaleStatusView) — there
    is no stored flag anywhere, so every check re-derives the answer from
    the log fresh; the click itself is never trusted on its own.

    Not yet validated against a real device's log all the way through a
    successful login (only the still-pending state has been observed
    directly) — worth confirming the True case reads correctly once
    tested against a real box.
    """
    tail = await _async_fetch_log_tail(hass, slug)
    if tail is None:
        return None
    return _LOGIN_URL_RE.search(tail) is None


async def async_get_latest_login_url(hass: HomeAssistant, slug: str) -> str | None:
    """The most recent pending-login URL in the add-on's log, or None if
    none is found in the recent tail (either already authenticated, or the
    add-on hasn't logged one yet). Always the LAST match, never the
    first — each retry cycle invalidates the previous code, and the
    dashboard's Authenticate button needs a URL that's still good the
    moment the customer actually taps it (see tailscale_http.py)."""
    tail = await _async_fetch_log_tail(hass, slug)
    if tail is None:
        return None
    matches = _LOGIN_URL_RE.findall(tail)
    return matches[-1] if matches else None
