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

import asyncio
import logging
import os
import re

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_SUPERVISOR_BASE_URL = "http://supervisor"
_REQUEST_TIMEOUT = ClientTimeout(total=10)

# A Supervisor add-on restart isn't instant — confirmed against a real
# device: from the add-on's own boot log, about 12 seconds elapsed
# between the container starting and tailscaled printing a fresh AuthURL
# (it calls tailscale up interactively on its own during startup, no
# button click needed — see async_restart_tailscale_addon's docstring).
# 45s leaves real margin over that without hanging a customer's
# Authenticate tap indefinitely if Supervisor itself is just slow.
_RESTART_TIMEOUT = ClientTimeout(total=45)

# How long async_trigger_fresh_login_url polls the log for the restart's
# fresh URL to show up, after the restart call itself returns: 10 tries,
# 2s apart, comfortably past the ~12s observed in testing.
_POLL_INTERVAL_SECONDS = 2
_POLL_ATTEMPTS = 10

# Matches this add-on's slug regardless of which repository it was
# installed from — the human-readable name always ends in "tailscale", but
# the leading segment is a hash of the add-on repository's URL (confirmed
# against the provisioner's own catalog: "a0d7b954_tailscale" for the Home
# Assistant Community Add-ons repository), so it's looked up rather than
# hardcoded.
_TAILSCALE_SLUG_SUFFIX = "_tailscale"

# Fallback match for the plain, untimestamped
#     To authenticate, visit:
#         https://login.tailscale.com/a/<code>
# block tailscaled also prints alongside the timestamped line below — kept
# in case a build/log format ever omits the timestamped one.
_LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+")

# The timestamped line tailscaled itself logs the moment control hands it
# a fresh auth URL — confirmed against a real add-on restart:
#     2026/09/18 17:46:24 control: AuthURL is https://login.tailscale.com/a/1d204523010f10
# Preferred over _LOGIN_URL_RE above because the timestamp is a genuine
# signal of how old this URL is, not just that a URL exists somewhere in
# the tail — even though nothing here currently does date-math on it (see
# async_trigger_fresh_login_url's docstring for why: Tailscale doesn't
# publish how long this link stays valid, so rather than guess a
# staleness threshold, the login flow always forces a fresh one).
_TIMESTAMPED_AUTH_URL_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} control: AuthURL is "
    r"(https://login\.tailscale\.com/a/[a-zA-Z0-9]+)",
    re.MULTILINE,
)

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

    Heuristic, not an authoritative API: a device that's still waiting on
    login has an auth URL sitting in its log (from its own startup, or
    from the restart async_trigger_fresh_login_url forces — see that
    function), so its absence from a reasonably-sized recent tail of the
    log is a good proxy for "already authenticated". This is exactly what
    the onboarding banner's "Done" button triggers (see tailscale_http.py's
    TailscaleStatusView) — there is no stored flag anywhere, so every
    check re-derives the answer from the log fresh; the click itself is
    never trusted on its own.

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
    add-on hasn't logged one yet). Prefers the timestamped
    "control: AuthURL is ..." line (_TIMESTAMPED_AUTH_URL_RE) over the
    plain "To authenticate, visit:" block, falling back to the latter only
    if the former isn't present. Always the LAST match, never the first —
    a restart invalidates whatever code came before it.

    This is a passive read only — it does not itself generate a URL. The
    dashboard's Authenticate button goes through
    async_trigger_fresh_login_url instead, which forces a fresh one before
    reading it back with this function."""
    tail = await _async_fetch_log_tail(hass, slug)
    if tail is None:
        return None
    timestamped_matches = _TIMESTAMPED_AUTH_URL_RE.findall(tail)
    if timestamped_matches:
        return timestamped_matches[-1]
    matches = _LOGIN_URL_RE.findall(tail)
    return matches[-1] if matches else None


async def async_restart_tailscale_addon(hass: HomeAssistant, slug: str) -> bool:
    """Restarts the add-on via Supervisor's own, fully documented add-on
    API — no reverse-engineered ingress session or click replication
    needed. Confirmed against a real restart: within about 12 seconds of
    a fresh boot, the add-on's own startup sequence calls tailscale up
    interactively on its own and prints a brand new AuthURL to the log,
    with no button click involved at all.

    Returns False on any failure (no Supervisor/token, add-on not found,
    Supervisor rejected the restart); callers treat that as "couldn't get
    a fresh link right now," not a fatal error."""
    headers = _auth_headers()
    if headers is None:
        return False

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"{_SUPERVISOR_BASE_URL}/addons/{slug}/restart",
            headers=headers,
            timeout=_RESTART_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Supervisor restart for %s returned HTTP %s", slug, resp.status)
                return False
    except (ClientError, TimeoutError) as err:
        _LOGGER.debug("Couldn't restart %s via Supervisor: %s", slug, err)
        return False
    return True


async def async_trigger_fresh_login_url(hass: HomeAssistant, slug: str) -> str | None:
    """The FALLBACK path only — called from tailscale_http.py's
    TailscaleLoginUrlView only after a plain, passive log read
    (async_get_latest_login_url) already came up empty. Restarts the
    add-on to force a brand new login-interactive attempt, then polls the
    log until the fresh AuthURL line shows up.

    This used to be the ONLY path (always restart, never trust whatever
    was already in the log) — reworked after a restart it triggered live
    left this add-on crashed in Supervisor's "error" state: its ingress
    MagicDNS proxy failed to rebind its own tailnet IP ("Address in use")
    right after the restart, s6 treated that as fatal and tore the whole
    add-on down, and since this add-on ships with watchdog off, Supervisor
    never brought it back on its own — a plain `ha addons start` on the
    same box reproduced the identical crash minutes later, so this isn't
    a one-off race, it's a real, repeatable risk on at least this
    hardware/version combination. A restart is no longer the default for
    that reason; it only runs when a passive read has nothing to work
    with, which itself should be uncommon, since the add-on's own startup
    sequence already prints a fresh URL automatically — a customer
    authenticating reasonably soon after provisioning will usually find
    one already sitting in the log with no restart needed at all.

    Returns None if the restart itself failed, or if no URL showed up in
    the log within the poll budget (_POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS
    on top of however long the restart call itself took). Callers should
    treat a restart-triggered result as carrying real risk to the add-on
    (see TailscaleLoginUrlView's "restarted" response field) — this
    function does not attempt to self-heal a crash it causes."""
    if not await async_restart_tailscale_addon(hass, slug):
        return None
    for _ in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        url = await async_get_latest_login_url(hass, slug)
        if url is not None:
            return url
    return None
