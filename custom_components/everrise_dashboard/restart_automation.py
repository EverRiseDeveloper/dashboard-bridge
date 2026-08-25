"""Seeds the three automations that make both bridge AND dashboard-frontend
updates fully hands-off, into every client's automations.yaml the first time
this integration is set up — same one-time, never-overwrite pattern
__init__.py already uses for default_config.json.

Three separate automations, not one, because "download" and "restart" have
very different risk profiles and shouldn't share a trigger, and because the
bridge (Python code, needs a restart to actually load) and the dashboard
frontend (static files in www/, live the moment they're written) don't
either:

1. Auto-install bridge update — fires the instant HACS's own update entity
   for this repo reports a new release, and just calls `update.install` on
   it. Cheap and harmless to do immediately: it only writes files to disk,
   same as a client clicking Update themselves.
2. Restart-if-pending — fires at 3am, but only restarts if the bridge's
   own "restart required" sensor (binary_sensor.py) says the files on disk
   don't match what's actually loaded yet. A full HA restart is genuinely
   disruptive (drops every entity in the house briefly), so it's worth
   gating on both a quiet-hours schedule AND "is there actually anything
   to activate" — a client who restarts manually before 3am leaves that
   sensor already off, and this does nothing.
3. Auto-install dashboard frontend update — fires the instant update.py's
   own coordinator (polling every 15 minutes — see update.py) reports a new
   dashboard-dist build, and calls `update.install` on it. No restart
   companion needed here: frontend_updater.install_latest() only swaps
   files in www/, which every subsequent page load just picks up — there's
   no Python import to go stale the way there is for the bridge itself.

Why automations.yaml rather than something this integration just does in
Python directly: a client should be able to see, disable, or delete any of
these like any other automation — not have it be invisible behavior baked
into the integration. automations.yaml is a plain, user-facing YAML file
(not `.storage/`, which is Home Assistant's internal state) — the same
file the Automation Editor UI itself reads and writes; this only differs in
doing it once, programmatically, on first setup.

Assumes the default HAOS onboarding layout (`automation: !include
automations.yaml` in configuration.yaml). A client whose configuration.yaml
doesn't include automations.yaml that way won't have these picked up —
same as any other YAML-file automation would need adding by hand in that
case.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Fixed and stable across every client — how we recognize "already seeded"
# (or "the client kept it") on every future setup. binary_sensor.py's
# entity, HACS's own generated bridge update entity, and update.py's own
# frontend update entity all have deterministic, device-less slugs (each is
# the only entity of its kind on a single-instance-per-client config entry),
# so hardcoding their entity_ids here is safe.
RESTART_REQUIRED_SENSOR = "binary_sensor.bridge_restart_required"
BRIDGE_UPDATE_ENTITY = "update.everrise_dashboard_config_bridge_update"
FRONTEND_UPDATE_ENTITY = "update.dashboard_frontend"

AUTO_INSTALL_AUTOMATION_ID = "everrise_dashboard_auto_install_bridge_update"
RESTART_AUTOMATION_ID = "everrise_dashboard_restart_if_pending"
AUTO_INSTALL_FRONTEND_AUTOMATION_ID = "everrise_dashboard_auto_install_frontend_update"

SEEDED_AUTOMATIONS = [
    {
        "id": AUTO_INSTALL_AUTOMATION_ID,
        "alias": "Auto-install bridge update",
        "description": (
            "Installs a new bridge release the moment HACS reports one is available — no "
            "click needed. Restarting to actually load it is handled separately by "
            "\"Restart HA for pending bridge update\" (3am, gated on the restart-required "
            "sensor), since that's disruptive enough to deserve its own schedule. Seeded "
            "automatically by the bridge on first setup; edit or delete it like any other "
            "automation — it's never re-created once it exists."
        ),
        "triggers": [{"trigger": "state", "entity_id": BRIDGE_UPDATE_ENTITY, "to": "on"}],
        "conditions": [],
        "actions": [{"action": "update.install", "target": {"entity_id": BRIDGE_UPDATE_ENTITY}}],
        "mode": "single",
    },
    {
        "id": RESTART_AUTOMATION_ID,
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
    },
    {
        "id": AUTO_INSTALL_FRONTEND_AUTOMATION_ID,
        "alias": "Auto-install dashboard frontend update",
        "description": (
            "Installs a new dashboard build (from dashboard-dist) the moment it's detected — no "
            "click needed, and nothing to restart: it only swaps static files in www/, which the "
            "next dashboard page load picks up on its own. Seeded automatically by the bridge on "
            "first setup; edit or delete it like any other automation — it's never re-created "
            "once it exists."
        ),
        "triggers": [{"trigger": "state", "entity_id": FRONTEND_UPDATE_ENTITY, "to": "on"}],
        "conditions": [],
        "actions": [{"action": "update.install", "target": {"entity_id": FRONTEND_UPDATE_ENTITY}}],
        "mode": "single",
    },
]


def _seed(automations_path: Path) -> bool:
    """Runs off the event loop (file I/O) — see __init__.py's call site.
    Returns True only if at least one new entry was actually written, so
    the caller knows whether an `automation.reload` is worth doing. Each
    of SEEDED_AUTOMATIONS is checked independently by its own fixed id, so
    a client who deleted just one of the three still only gets that one
    re-seeded — never the others, and never a duplicate of the ones they
    kept."""
    try:
        existing = yaml.safe_load(automations_path.read_text(encoding="utf-8")) if automations_path.exists() else []
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Couldn't read %s to seed automations: %s", automations_path, err)
        return False

    if existing is None:
        existing = []
    if not isinstance(existing, list):
        _LOGGER.warning(
            "%s isn't a plain list of automations — skipping the automation seed "
            "rather than risk corrupting whatever's actually there. Add them by hand instead.",
            automations_path,
        )
        return False

    existing_ids = {item.get("id") for item in existing if isinstance(item, dict)}
    to_add = [cfg for cfg in SEEDED_AUTOMATIONS if cfg["id"] not in existing_ids]
    if not to_add:
        return False  # Already seeded (or the client kept/edited them) — never touch again.

    existing.extend(to_add)
    try:
        automations_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = automations_path.with_name(f"{automations_path.name}.tmp-{uuid4().hex}")
        tmp.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        os.replace(tmp, automations_path)  # atomic on POSIX and Windows
    except OSError as err:
        _LOGGER.error("Failed seeding automations at %s: %s", automations_path, err)
        return False

    _LOGGER.info("Seeded %d automation(s) at %s", len(to_add), automations_path)
    return True


async def async_seed_restart_automation_if_missing(hass: HomeAssistant) -> None:
    automations_path = Path(hass.config.path("automations.yaml"))
    seeded = await hass.async_add_executor_job(_seed, automations_path)
    if not seeded:
        return

    # Best-effort: this is purely to make a newly-seeded automation live
    # immediately, the same convenience the UI editor gets when you save
    # through it — not something worth failing our own async_setup_entry
    # over. It genuinely did fail once already: on a fresh setup where the
    # `automation` integration hadn't finished loading yet (we didn't
    # declare it as a manifest dependency), `automation.reload` wasn't
    # registered yet, and letting ServiceNotFound propagate here took the
    # ENTIRE bridge config entry down with it — every platform, the HTTP
    # API, everything — over a nice-to-have. The file write above already
    # succeeded regardless, so the seeded automation(s) will be picked up
    # on the very next restart no matter what happens here.
    try:
        await hass.services.async_call("automation", "reload")
    except HomeAssistantError as err:
        _LOGGER.warning(
            "Seeded automation(s) will take effect on the next restart instead of immediately: %s", err
        )
