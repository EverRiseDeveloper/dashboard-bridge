"""Update entity for the dashboard's compiled frontend.

Surfaces "a new dashboard build is available" the way Home Assistant
already shows OS, Core, and HACS updates — Settings -> System -> Updates,
with an Install button and a sidebar notification badge — rather than a
bespoke screen inside the dashboard app itself. The actual download/extract
work lives in frontend_updater.py; this just wires it into HA's update
platform and keeps the "latest known build" fresh via a DataUpdateCoordinator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DIST_REPO_NAME, DIST_REPO_OWNER, DOMAIN
from .frontend_updater import get_installed_build_id, get_latest_build_id, install_latest

_LOGGER = logging.getLogger(__name__)

# How often to ask GitHub whether a new build exists. Cheap (one small
# version.json fetch), so this doesn't need to be aggressive — a client
# clicking "Check for updates" in HA's own Updates page also triggers an
# immediate refresh regardless of this interval.
_CHECK_INTERVAL = timedelta(hours=6)


def _format_build_id(build_id: str | None) -> str | None:
    """buildId is a raw millisecond-epoch timestamp (see the dashboard
    repo's vite.config.ts, where it's generated as `String(Date.now())`) —
    shown as a readable build date instead of a 13-digit number, since
    that's what actually renders in HA's Updates UI."""
    if build_id is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(build_id) / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return build_id
    return dt.strftime("%Y-%m-%d %H:%M UTC")


class _LatestBuildCoordinator(DataUpdateCoordinator[str | None]):
    """Polls dashboard-dist's version.json — see frontend_updater.get_latest_build_id."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_latest_build", update_interval=_CHECK_INTERVAL)

    async def _async_update_data(self) -> str | None:
        return await get_latest_build_id(self.hass)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = _LatestBuildCoordinator(hass)
    # Best-effort: a failed first check just leaves latest_version "unknown"
    # until the next scheduled poll, not a broken entity — GitHub being
    # briefly unreachable shouldn't block the rest of setup.
    await coordinator.async_refresh()
    async_add_entities([EverriseDashboardUpdateEntity(hass, entry, coordinator)])


class EverriseDashboardUpdateEntity(CoordinatorEntity[_LatestBuildCoordinator], UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Dashboard frontend"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_release_url = f"https://github.com/{DIST_REPO_OWNER}/{DIST_REPO_NAME}"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: _LatestBuildCoordinator) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_frontend_update"
        self._installed_build_id: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._installed_build_id = await get_installed_build_id(self._hass)
        self.async_write_ha_state()

    @property
    def installed_version(self) -> str | None:
        return _format_build_id(self._installed_build_id)

    @property
    def latest_version(self) -> str | None:
        return _format_build_id(self.coordinator.data)

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        if not await install_latest(self._hass):
            raise HomeAssistantError(
                "Couldn't install the latest dashboard build — check the Home Assistant log for details."
            )
        self._installed_build_id = await get_installed_build_id(self._hass)
        self.async_write_ha_state()
