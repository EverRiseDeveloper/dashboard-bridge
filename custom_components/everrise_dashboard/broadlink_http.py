"""Authenticated, admin-only list of what each Broadlink remote has learnt,
for the dashboard's Admin → Rooms fan speed fields (DeviceListEditor.tsx in
the dashboard repo). The fan's name in the remote is picked from this list
instead of being typed: one wrong letter, or a phone capitalising the first
one, meant "Command not found" from the remote and a fan that ignored its
buttons.

Home Assistant has no API for this. The Broadlink integration keeps what it
has learnt in its own storage file, `.storage/broadlink_remote_<mac>_codes`
(homeassistant/components/broadlink/remote.py), and says nothing about it
on the entity. This reads that file through Home Assistant's own Store
helper — never saved back — and returns names only. The codes themselves
never leave the box: an RF code learnt for a gate or a garage door opens it
when it's replayed.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# The Broadlink integration's own names for its codes file. Its remote
# entity's unique_id is the device's unique_id (the MAC address), the same
# one the file is named after.
_CODES_STORAGE_VERSION = 1
_CODES_STORAGE_KEY = "broadlink_remote_{}_codes"


def learnt_names(data: object) -> dict[str, list[str]]:
    """Device name → its command names, in the order they were learnt, from
    the codes file's data ({device: {command: code or [code, code]}}).
    Anything shaped otherwise is left out rather than trusted."""
    if not isinstance(data, dict):
        return {}
    devices: dict[str, list[str]] = {}
    for device, commands in data.items():
        if isinstance(device, str) and device and isinstance(commands, dict):
            devices[device] = [name for name in commands if isinstance(name, str) and name]
    return devices


async def async_learnt_remotes(hass: HomeAssistant) -> dict[str, dict[str, list[str]]]:
    """Every Broadlink remote entity → what it has learnt. A remote whose
    file is missing (nothing learnt yet) or unreadable lists nothing."""
    remotes: dict[str, dict[str, list[str]]] = {}
    for entry in list(er.async_get(hass).entities.values()):
        if entry.platform != "broadlink" or not entry.entity_id.startswith("remote.") or not entry.unique_id:
            continue
        store: Store[dict] = Store(hass, _CODES_STORAGE_VERSION, _CODES_STORAGE_KEY.format(entry.unique_id))
        try:
            data = await store.async_load()
        except Exception:  # noqa: BLE001 — a damaged file, or a newer format than this knows
            _LOGGER.warning("Couldn't read what %s has learnt", entry.entity_id, exc_info=True)
            data = None
        remotes[entry.entity_id] = learnt_names(data)
    return remotes


class BroadlinkLearntView(HomeAssistantView):
    """GET what the Broadlink remotes have learnt: names, never codes.

    Admin only, like saving the dashboard config — the only thing that uses
    it is the Admin page, and only an admin can save what's picked there.
    """

    url = "/api/everrise_dashboard/broadlink/learnt"
    name = "api:everrise_dashboard:broadlink:learnt"
    # requires_auth defaults to True on the base class — left unset deliberately.

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        hass_user = request.get("hass_user")
        if hass_user is None or not hass_user.is_admin:
            return self.json_message(
                "Only an admin user can see what the remotes have learnt.", HTTPStatus.FORBIDDEN
            )
        return self.json({"remotes": await async_learnt_remotes(self._hass)})
