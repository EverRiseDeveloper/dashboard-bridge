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
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    async_add_entities([_BridgeVersionSensor(entry, integration.manifest.get("version"))])


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
