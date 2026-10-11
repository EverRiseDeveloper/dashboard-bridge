"""Flags when this integration's own Python code has been updated on disk
but Home Assistant hasn't been restarted to actually load it yet.

The dashboard's own files (www/) take effect without any restart. This
bridge's own .py files are a normal Python module: an install from the
Software update screen overwrites them on disk, but the already-running
process keeps executing whatever it imported at startup until Home
Assistant is actually restarted — not reloaded. The dashboard shows
"Restart needed" while this is on.

Neither HACS's own bookkeeping nor a plain "is a HACS update pending" check
can answer "has tonight's restart already happened" reliably: HACS marks a
repository as fully installed/up to date as soon as it finishes writing the
new files to disk, which is before a restart, not after one. So a naive
"restart if HACS shows a pending update" automation would restart every
single night forever, and a naive "restart once" automation can't tell a
scheduled restart apart from a client manually restarting on their own
first. This entity answers the actual question directly: it compares the
version this running process loaded at startup against a fresh read of
manifest.json straight off disk, right now. The two only differ in the
window between "an update wrote new files" and "Home Assistant was next
restarted", however that restart happened.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# A local file read, not a network call — no need to poll aggressively.
# An install nudges it straight away anyway (update_manager.py).
SCAN_INTERVAL = timedelta(minutes=5)


def _read_disk_version(manifest_path: Path) -> str | None:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return str(version) if version is not None else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    # async_get_integration() returns the manifest HA actually loaded into
    # memory at startup — not a fresh disk read — which is exactly the
    # "what's really running" baseline this needs to compare against.
    integration = await async_get_integration(hass, DOMAIN)
    loaded_version = integration.manifest.get("version")
    manifest_path = Path(integration.file_path) / "manifest.json"
    async_add_entities([_RestartRequiredSensor(entry, loaded_version, manifest_path)])


class _RestartRequiredSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Bridge restart required"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:restart-alert"

    def __init__(self, entry: ConfigEntry, loaded_version: str | None, manifest_path: Path) -> None:
        self._attr_unique_id = f"{entry.entry_id}_bridge_restart_required"
        self._loaded_version = loaded_version
        self._manifest_path = manifest_path
        self._attr_is_on = False

    async def async_update(self) -> None:
        # File I/O off the event loop, same reasoning as everywhere else in
        # this integration that touches disk (frontend_updater.py, storage.py).
        disk_version = await self.hass.async_add_executor_job(_read_disk_version, self._manifest_path)
        # None (unreadable manifest) deliberately does NOT flag restart-required
        # — a transient read glitch shouldn't show "Restart needed" on its own.
        self._attr_is_on = disk_version is not None and disk_version != self._loaded_version
