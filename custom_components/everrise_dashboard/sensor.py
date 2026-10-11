"""Reports which version of this bridge is actually running.

Until HACS was dropped in favour of the EverRise Auto Updater add-on, HACS
generated an `update.everrise_dashboard_config_bridge_update` entity, and
that was the only place a version for this integration appeared anywhere in
Home Assistant. Nothing replaced it: Supervisor doesn't version custom
integrations, and the updater add-on can't help either — it deploys by
comparing git commit hashes and never parses manifest.json, so its own
add-on version (config.yaml's `version`, what Settings > Add-ons shows)
describes the updater program, not what the updater put on the box.

The result was no way at all to answer "which bridge is this client on?"
short of reading manifest.json over Samba. This closes that.

Deliberately reported from the RUNNING process rather than published
anywhere upstream: `async_get_integration()` returns the manifest Home
Assistant actually loaded at startup, so this is ground truth for this box
specifically. It stays correct when the updater wasn't involved at all —
a hand-copied deploy over Samba or Studio Code Server included — which a
publisher-side version number never would.

Pairs with binary_sensor.py: this says what's loaded, that says whether
something newer is already sitting on disk waiting for a restart. The
dashboard's own Admin > About tab reads both.

Also here: the EverRise software update sensor — "up_to_date",
"available", "installing" or "unknown" — which the dashboard reads for the
badge on the Software update row and to grey out Install while someone
else's install is running. It's fed by update_runtime.py (the background
check in update_notifier.py, and installs in update_manager.py), so it
changes live without any phone asking GitHub itself.
"""

from __future__ import annotations

import logging

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .update_runtime import STATUS_OPTIONS, UpdateRuntime, get_update_runtime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    async_add_entities(
        [
            _BridgeVersionSensor(entry, integration.manifest.get("version")),
            _SoftwareUpdateSensor(entry, get_update_runtime(hass)),
        ]
    )


class _BridgeVersionSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Bridge version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:package-variant"
    # The version this process loaded cannot change while the process is
    # alive — a new one on disk only takes effect at the next restart, which
    # is precisely what binary_sensor.py's restart-required entity is for.
    # So there is nothing to poll for here.
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, loaded_version: str | None) -> None:
        self._attr_unique_id = f"{entry.entry_id}_bridge_version"
        # "unknown" rather than None so the entity always has a readable
        # state — a manifest with no version is a packaging mistake worth
        # seeing in the UI, not something to hide as an empty value.
        self._attr_native_value = loaded_version or "unknown"


class _SoftwareUpdateSensor(SensorEntity):
    """sensor.everrise_software_update — see the module docstring."""

    _attr_has_entity_name = True
    _attr_name = "EverRise software update"
    _attr_icon = "mdi:package-up"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    # Pushed from update_runtime.py on every change; nothing to poll.
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: UpdateRuntime) -> None:
        self._attr_unique_id = f"{entry.entry_id}_software_update"
        self._runtime = runtime

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._runtime.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._runtime.status()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._runtime.attributes()
