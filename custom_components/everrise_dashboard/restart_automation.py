"""Seeds the two automations that make dashboard-frontend and bridge
updates fully hands-off, into every client's automations.yaml the first time
this integration is set up — same one-time, never-overwrite pattern
__init__.py already uses for default_config.json. It also prunes two older
automations that no longer do anything (see below).

Two separate automations, not one, because the bridge (Python code, needs a
restart to actually load) and the dashboard frontend (static files in www/,
live the moment they're written) don't behave the same way:

1. Auto-install dashboard frontend update — fires the instant update.py's
   own coordinator (polling every 15 minutes — see update.py, which this
   integration owns outright) reports a new dashboard-dist build, and calls
   `update.install` on it. No restart companion needed: install_latest()
   only swaps files in www/, which every subsequent page load picks up —
   there's no Python import to go stale the way there is for the bridge.
2. Restart-if-pending — fires at 3am, but only restarts if the bridge's own
   "restart required" sensor (binary_sensor.py) says the files on disk don't
   match what's actually loaded yet. This is now a SAFETY NET rather than
   the main path: the EverRise Auto Updater add-on restarts Core itself the
   moment it deploys a new bridge. But look at run.sh's restart_core() — the
   POST is `|| true` and a no-response is only a warning, so a silently
   failed restart would otherwise leave new code sitting unloaded
   indefinitely. This catches exactly that, and does nothing on any night
   where the add-on's own restart worked (the sensor is already off).

## Pruned: the two HACS-era automations

Before the updater add-on existed, the bridge was distributed through HACS,
and two more automations existed to drive HACS's own generated update entity
(`update.everrise_dashboard_config_bridge_update`): one to force-refresh it
every 30 minutes (HACS's background scan for a custom repository can go ~48
hours between checks), and one to auto-install whatever it reported.

Neither works without HACS — the entity simply doesn't exist — and the
force-check one is actively noisy about it, logging "Forced update failed.
Entity ... not found" every 30 minutes (observed on Client_003). So they are
no longer seeded, and LEGACY_HACS_AUTOMATION_IDS below removes them from
boxes that already have them.

That removal is conditional on HACS actually being absent, deliberately: a
box that still has HACS installed (the dev box) has a working bridge update
entity, so those two automations still function there and are left exactly
as they are. Nothing here needs per-box configuration to get that right.

Why automations.yaml rather than something this integration just does in
Python directly: a client should be able to see, disable, or delete any of
these like any other automation — not have it be invisible behavior baked
into the integration. automations.yaml is a plain, user-facing YAML file
(not `.storage/`, which is Home Assistant's internal state) — the same file
the Automation Editor UI itself reads and writes; this only differs in doing
it once, programmatically, on first setup.

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
FRONTEND_UPDATE_ENTITY = "update.dashboard_frontend"

FORCE_CHECK_BRIDGE_AUTOMATION_ID = "everrise_dashboard_force_check_bridge_update"
AUTO_INSTALL_AUTOMATION_ID = "everrise_dashboard_auto_install_bridge_update"
RESTART_AUTOMATION_ID = "everrise_dashboard_restart_if_pending"
AUTO_INSTALL_FRONTEND_AUTOMATION_ID = "everrise_dashboard_auto_install_frontend_update"

# Seeded by earlier versions of this file, back when the bridge shipped
# through HACS. Both drive an update entity HACS generated, so on a box
# without HACS they are dead — and the force-check one logs a failure every
# 30 minutes. Removed by _sync() below, but only where HACS is genuinely
# absent; see the module docstring.
LEGACY_HACS_AUTOMATION_IDS = frozenset(
    {FORCE_CHECK_BRIDGE_AUTOMATION_ID, AUTO_INSTALL_AUTOMATION_ID}
)

SEEDED_AUTOMATIONS = [
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


def _sync(automations_path: Path, prune_ids: frozenset[str]) -> bool:
    """Runs off the event loop (file I/O) — see __init__.py's call site.

    Does both jobs in a single read/write pass: adds any of
    SEEDED_AUTOMATIONS this box doesn't already have, and drops any entry
    whose id is in prune_ids. Returns True only if the file actually
    changed, so the caller knows whether an `automation.reload` is worth
    doing.

    Each of SEEDED_AUTOMATIONS is checked independently by its own fixed
    id, so a client who deleted just one of them still only gets that one
    re-seeded — never the others, and never a duplicate of the ones they
    kept.
    """
    try:
        existing = yaml.safe_load(automations_path.read_text(encoding="utf-8")) if automations_path.exists() else []
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Couldn't read %s to sync automations: %s", automations_path, err)
        return False

    if existing is None:
        existing = []
    if not isinstance(existing, list):
        _LOGGER.warning(
            "%s isn't a plain list of automations — skipping the automation sync "
            "rather than risk corrupting whatever's actually there. Add them by hand instead.",
            automations_path,
        )
        return False

    # Prune first, so an id that is somehow in BOTH prune_ids and
    # SEEDED_AUTOMATIONS would end up re-seeded rather than silently
    # deleted. Nothing is in both today; this just makes the ordering
    # deliberate rather than incidental.
    kept: list[object] = []
    pruned: list[str] = []
    for item in existing:
        if isinstance(item, dict) and item.get("id") in prune_ids:
            pruned.append(str(item.get("alias") or item.get("id")))
            continue
        kept.append(item)

    existing_ids = {item.get("id") for item in kept if isinstance(item, dict)}
    to_add = [cfg for cfg in SEEDED_AUTOMATIONS if cfg["id"] not in existing_ids]

    if not to_add and not pruned:
        return False  # Nothing to seed, nothing to remove — never touch the file.

    kept.extend(to_add)
    try:
        automations_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = automations_path.with_name(f"{automations_path.name}.tmp-{uuid4().hex}")
        tmp.write_text(
            yaml.safe_dump(kept, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        os.replace(tmp, automations_path)  # atomic on POSIX and Windows
    except OSError as err:
        _LOGGER.error("Failed syncing automations at %s: %s", automations_path, err)
        return False

    if to_add:
        _LOGGER.info("Seeded %d automation(s) at %s", len(to_add), automations_path)
    if pruned:
        _LOGGER.info(
            "Removed %d automation(s) that no longer work without HACS (%s) from %s",
            len(pruned),
            ", ".join(pruned),
            automations_path,
        )
    return True


def _hacs_installed_on_disk(hass: HomeAssistant) -> bool:
    """Runs off the event loop — a plain existence check on the config dir."""
    return Path(hass.config.path("custom_components/hacs")).exists()


async def async_sync_seeded_automations(hass: HomeAssistant) -> None:
    automations_path = Path(hass.config.path("automations.yaml"))

    # Two independent probes, because neither is reliable alone this early
    # in startup. A persisted config entry is in the registry regardless of
    # whether HACS itself has finished setting up, so it doesn't depend on
    # load order the way hass.states or hass.config.components would; the
    # on-disk check then covers an install that hasn't produced an entry.
    # Erring towards "HACS is here" is the safe direction — it means
    # leaving the legacy automations alone rather than deleting ones that
    # still do something.
    hacs_present = bool(hass.config_entries.async_entries("hacs")) or await hass.async_add_executor_job(
        _hacs_installed_on_disk, hass
    )
    if hacs_present:
        _LOGGER.debug(
            "HACS is installed on this instance — leaving the legacy bridge-update automations in place"
        )
    prune_ids = frozenset() if hacs_present else LEGACY_HACS_AUTOMATION_IDS

    changed = await hass.async_add_executor_job(_sync, automations_path, prune_ids)
    if not changed:
        return

    # Best-effort: this is purely to make a newly-seeded (or newly-removed)
    # automation take effect immediately, the same convenience the UI editor
    # gets when you save through it — not something worth failing our own
    # async_setup_entry over. It genuinely did fail once already: on a fresh
    # setup where the `automation` integration hadn't finished loading yet
    # (we didn't declare it as a manifest dependency), `automation.reload`
    # wasn't registered yet, and letting ServiceNotFound propagate here took
    # the ENTIRE bridge config entry down with it — every platform, the HTTP
    # API, everything — over a nice-to-have. The file write above already
    # succeeded regardless, so the change will be picked up on the very next
    # restart no matter what happens here.
    try:
        await hass.services.async_call("automation", "reload")
    except HomeAssistantError as err:
        _LOGGER.warning(
            "Automation changes will take effect on the next restart instead of immediately: %s", err
        )
