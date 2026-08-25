"""Seeds a "restart at 3am, but only if actually pending" automation into
every client's automations.yaml the first time this integration is set up —
same one-time, never-overwrite pattern __init__.py already uses for
default_config.json.

Why an automation in automations.yaml rather than something this integration
just does in Python directly: a plain scheduled call to `homeassistant.restart`
from inside our own code would restart HA every night whether or not there
was anything to activate, and a client should be able to see, disable, or
delete this like any other automation — not have it be an invisible behavior
baked into the integration. Writing to automations.yaml (a plain, user-facing
YAML file — not `.storage/`, which is Home Assistant's internal state) is the
same file the Automation Editor UI itself reads and writes; this only differs
in doing it once, programmatically, on first setup.

Assumes the default HAOS onboarding layout (`automation: !include
automations.yaml` in configuration.yaml). A client whose configuration.yaml
doesn't include automations.yaml that way won't have this picked up — same
as any other YAML-file automation would need adding by hand in that case.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

import yaml

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Fixed and stable across every client — this is how we recognize "already
# seeded" (or "the client kept it") on every future setup, and how the 3am
# time trigger's gating sensor is addressed. binary_sensor.py's entity has
# no device grouping, so its slug is deterministic across every install.
AUTOMATION_ID = "everrise_dashboard_restart_if_pending"
RESTART_REQUIRED_SENSOR = "binary_sensor.bridge_restart_required"

AUTOMATION_CONFIG = {
    "id": AUTOMATION_ID,
    "alias": "Restart HA for pending bridge update",
    "description": (
        "Only restarts if the Everrise bridge's own Python code was updated by HACS but "
        "never actually loaded (see the bridge's \"Bridge restart required\" binary sensor) "
        "— if this client already restarted manually earlier, that sensor is already off "
        "and this does nothing at 3am. Seeded automatically by the bridge on first setup; "
        "edit or delete it like any other automation — it's never re-created once it exists."
    ),
    "triggers": [{"trigger": "time", "at": "03:00:00"}],
    "conditions": [{"condition": "state", "entity_id": RESTART_REQUIRED_SENSOR, "state": "on"}],
    "actions": [{"action": "homeassistant.restart"}],
    "mode": "single",
}


def _seed(automations_path: Path) -> bool:
    """Runs off the event loop (file I/O) — see __init__.py's call site.
    Returns True only if a new entry was actually written, so the caller
    knows whether an `automation.reload` is worth doing."""
    try:
        existing = yaml.safe_load(automations_path.read_text(encoding="utf-8")) if automations_path.exists() else []
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Couldn't read %s to seed the restart automation: %s", automations_path, err)
        return False

    if existing is None:
        existing = []
    if not isinstance(existing, list):
        _LOGGER.warning(
            "%s isn't a plain list of automations — skipping the restart-automation seed "
            "rather than risk corrupting whatever's actually there. Add it by hand instead.",
            automations_path,
        )
        return False
    if any(isinstance(item, dict) and item.get("id") == AUTOMATION_ID for item in existing):
        return False  # Already seeded (or the client kept/edited it) — never touch it again.

    existing.append(AUTOMATION_CONFIG)
    try:
        automations_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = automations_path.with_name(f"{automations_path.name}.tmp-{uuid4().hex}")
        tmp.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        os.replace(tmp, automations_path)  # atomic on POSIX and Windows
    except OSError as err:
        _LOGGER.error("Failed seeding the restart automation at %s: %s", automations_path, err)
        return False

    _LOGGER.info("Seeded the restart-if-pending automation at %s", automations_path)
    return True


async def async_seed_restart_automation_if_missing(hass: HomeAssistant) -> None:
    automations_path = Path(hass.config.path("automations.yaml"))
    seeded = await hass.async_add_executor_job(_seed, automations_path)
    if seeded:
        # Picks the new automation up immediately — no restart needed for
        # this part, same as any automation created through the UI editor.
        await hass.services.async_call("automation", "reload")
