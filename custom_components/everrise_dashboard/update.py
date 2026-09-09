"""Update entity for the dashboard's compiled frontend.

Surfaces "a new dashboard build is available" the way Home Assistant
already shows OS, Core, and HACS updates — Settings -> System -> Updates,
with an Install button and a sidebar notification badge — rather than a
bespoke screen inside the dashboard app itself. The actual download/extract
work lives in frontend_updater.py; this just wires it into HA's update
platform and keeps the "latest known version" fresh via a DataUpdateCoordinator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DIST_REPO_NAME, DIST_REPO_OWNER, DOMAIN
from .frontend_updater import fetch_latest_version, get_installed_version, install_latest

_LOGGER = logging.getLogger(__name__)

# How often to ask GitHub whether a new version exists. Cheap (one small
# version.json fetch, cache-busted with a query param), so there's no real
# cost to checking often — a client clicking "Check for updates" in HA's
# own Updates page also triggers an immediate refresh regardless of this
# interval. Used to be 6 hours; a fresh deploy-dist.yml build could then sit
# undetected for up to 6 hours with nothing but a manual "Check for updates"
# click to shortcut it — annoying enough in practice (twice, on Client_003)
# that a short poll plus the auto-install automation below (see
# restart_automation.py's AUTO_INSTALL_FRONTEND_AUTOMATION_ID) fully closes
# the loop instead: push a build, and it's live in www/ within minutes with
# no one needing to touch HA at all.
_CHECK_INTERVAL = timedelta(minutes=15)

# How many consecutive failed checks before this says so in the log. One
# blip is not worth a warning — GitHub being briefly unreachable is normal
# — but three in a row is ~45 minutes of flying blind, which is.
_FAILURE_WARN_AFTER = 3


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    """Parses a plain "MAJOR.MINOR.PATCH" string (an optional leading "v"
    is tolerated) into a comparable tuple — matches dashboard's own
    package.json version field (see vite.config.ts), which is what actually
    ends up in version.json. Doesn't attempt to handle pre-release/build
    metadata suffixes (e.g. "1.2.0-beta.1") — we control both ends of this
    version string, so it's kept to the simple three-number form on
    purpose. Anything else (unparseable, wrong number of parts) returns
    None rather than guessing, so callers can fall back to a safe default."""
    text = value[1:] if value[:1] in ("v", "V") else value
    parts = text.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None
    return (major, minor, patch)


def _latest_is_newer(latest: str | None, installed: str | None) -> bool:
    """Nothing installed yet (a fresh bootstrap that hasn't succeeded) still
    counts as "newer" so the Install button remains a manual retry path.
    Otherwise, parse both as semver and compare numerically — a plain
    string inequality would flag "update available" even when a *stale*
    read of dashboard-dist's version.json reports an OLDER release than
    what's already installed (raw.githubusercontent.com caches that file
    for a few minutes, more so right after deploy-dist.yml's force-push),
    which showed up in practice as a confusing "Update available" that
    installed nothing new because there was nothing new to install. Falls
    back to simple inequality only if either string doesn't parse as
    semver — shouldn't happen since both come from the same package.json,
    but don't silently hide a real update over a malformed value."""
    if installed is None:
        return latest is not None
    if latest is None:
        return False
    latest_tuple = _parse_semver(latest)
    installed_tuple = _parse_semver(installed)
    if latest_tuple is not None and installed_tuple is not None:
        return latest_tuple > installed_tuple
    return latest != installed


class _LatestVersionCoordinator(DataUpdateCoordinator[str | None]):
    """Polls dashboard-dist's version.json — see frontend_updater.fetch_latest_version.

    Also keeps track of when a check was last ATTEMPTED, when one last
    SUCCEEDED, and why the last attempt failed — all of which the entity
    exposes as attributes. That exists because of a genuinely invisible
    failure mode: the fetch returns None on any error (HTTP, timeout, bad
    JSON), None means "not newer" to _latest_is_newer, and latest_version
    echoes installed_version — so a box that cannot reach GitHub at all
    reports a confident "up to date" indefinitely.

    That bit us three times. First a client sat on an old build with no way
    to tell "hasn't polled yet" from "polling and failing every time"; the
    counters below fixed that. Then twice more with the counters in place,
    because they said only THAT it failed, never why — the reason was
    logged at debug and therefore invisible at the default level. On
    Client_003 it turned out to be "Cannot connect to host
    raw.githubusercontent.com:443 ssl:default [Network unreachable]",
    which points straight at the box's own routing and would have ended
    the investigation immediately had anyone been able to read it. So
    fetch_latest_version now hands the reason back, it goes into the
    warning below, and it's published as last_check_error for the
    dashboard's Admin > About tab.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_latest_version", update_interval=_CHECK_INTERVAL)
        self.last_success: datetime | None = None
        # Every ATTEMPT, unlike last_success — the two diverge precisely
        # during an outage, which is when someone is looking. It's also
        # what "next check at ..." has to be derived from: the coordinator
        # reschedules _CHECK_INTERVAL after each refresh regardless of
        # whether that refresh succeeded.
        self.last_check: datetime | None = None
        self.last_error: str | None = None
        self.failure_streak = 0

    @property
    def next_check(self) -> datetime | None:
        if self.last_check is None:
            return None
        return self.last_check + _CHECK_INTERVAL

    async def _async_update_data(self) -> str | None:
        latest, error = await fetch_latest_version(self.hass)
        self.last_check = dt_util.utcnow()

        if latest is None:
            self.failure_streak += 1
            self.last_error = error
            # Warn exactly once per outage (on the Nth failure, not every
            # one after it) so a long outage doesn't fill the log — but
            # carry the reason, which used to be debug-only. "3 failed
            # attempts" with no cause is the kind of telemetry that sends
            # you looking at DNS for twenty minutes.
            if self.failure_streak == _FAILURE_WARN_AFTER:
                _LOGGER.warning(
                    "Couldn't check for a new dashboard build %s times in a row (~%s minutes): %s. "
                    "The version reported may be stale; last successful check: %s",
                    self.failure_streak,
                    int(self.failure_streak * _CHECK_INTERVAL.total_seconds() // 60),
                    error or "no reason reported",
                    self.last_success.isoformat() if self.last_success else "never",
                )
            else:
                _LOGGER.debug("Couldn't check for a new dashboard build: %s", error)
            # Deliberately keep the last known-good value rather than
            # returning None: dropping it would make latest_version flap
            # back to installed_version and could re-fire the auto-install
            # automation on the next successful poll for no reason.
            return self.data

        self.failure_streak = 0
        self.last_error = None
        self.last_success = self.last_check
        return latest


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = _LatestVersionCoordinator(hass)
    # Best-effort: a failed first check just leaves latest_version "unknown"
    # until the next scheduled poll, not a broken entity — GitHub being
    # briefly unreachable shouldn't block the rest of setup.
    await coordinator.async_refresh()
    async_add_entities([EverriseDashboardUpdateEntity(hass, entry, coordinator)])


class EverriseDashboardUpdateEntity(CoordinatorEntity[_LatestVersionCoordinator], UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Dashboard frontend"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_release_url = f"https://github.com/{DIST_REPO_OWNER}/{DIST_REPO_NAME}"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: _LatestVersionCoordinator) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_frontend_update"
        self._installed_version: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._installed_version = await get_installed_version(self._hass)
        self.async_write_ha_state()

    @property
    def installed_version(self) -> str | None:
        return self._installed_version

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Enough to tell "checked, genuinely up to date" apart from
        "hasn't managed to check in hours" — see the coordinator's own
        docstring for why that distinction wasn't visible before."""
        return {
            "last_successful_check": self.coordinator.last_success,
            "failed_checks_since_success": self.coordinator.failure_streak,
            # last_check is every attempt and next_check is when the
            # following one is due, so the About tab can say "next check at
            # 17:56" instead of an ageing "checked 40 minutes ago" — which
            # reads like the check is broken even when it's merely between
            # polls, and says nothing at all about whether the last one
            # worked.
            "last_check": self.coordinator.last_check,
            "next_check": self.coordinator.next_check,
            # None whenever the most recent attempt succeeded, so the
            # frontend can treat "present" as "the last check failed, and
            # here is why".
            "last_check_error": self.coordinator.last_error,
            # Published so a consumer (the About tab) can decide what
            # counts as stale from the real interval rather than guessing.
            "check_interval_minutes": int(_CHECK_INTERVAL.total_seconds() // 60),
        }

    @property
    def latest_version(self) -> str | None:
        latest = self.coordinator.data
        if _latest_is_newer(latest, self._installed_version):
            return latest
        # Not actually newer than what's installed (or nothing to compare
        # yet) — echo the installed version so HA reports "up to date"
        # instead of a false positive from a stale/older CDN read.
        return self._installed_version

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        if not await install_latest(self._hass):
            raise HomeAssistantError(
                "Couldn't install the latest dashboard build — check the Home Assistant log for details."
            )
        self._installed_version = await get_installed_version(self._hass)
        self.async_write_ha_state()
