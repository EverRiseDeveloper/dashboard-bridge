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
from datetime import timedelta

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DIST_REPO_NAME, DIST_REPO_OWNER, DOMAIN
from .frontend_updater import get_installed_version, get_latest_version, install_latest

_LOGGER = logging.getLogger(__name__)

# How often to ask GitHub whether a new version exists. Cheap (one small
# version.json fetch), so this doesn't need to be aggressive — a client
# clicking "Check for updates" in HA's own Updates page also triggers an
# immediate refresh regardless of this interval.
_CHECK_INTERVAL = timedelta(hours=6)


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
    """Polls dashboard-dist's version.json — see frontend_updater.get_latest_version."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_latest_version", update_interval=_CHECK_INTERVAL)

    async def _async_update_data(self) -> str | None:
        return await get_latest_version(self.hass)


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
