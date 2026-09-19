"""Seeds the one automation that's still needed once an update actually
installs, and prunes every automation this integration used to seed for
two update mechanisms that no longer exist: the standalone "EverRise Auto
Updater" add-on, and (before that) driving HACS's own generated update
entity for the bridge. Same one-time, never-overwrite pattern __init__.py
already uses for default_config.json.

## What's still seeded: the 3am restart safety net

A backend install (see bridge_updater.py, reached through the dashboard's
own Updates screen — update_manager.py/update_http.py) writes new Python
files to disk, but this already-running process keeps executing whatever
it imported at startup until Home Assistant is actually restarted. The
Updates screen asks the household member who just installed whether to
restart right away; if they say "later" (or just close the tab), nothing
else prompts them again. This automation is that follow-through: it fires
at 3am, but only restarts if the bridge's own "restart required" sensor
(binary_sensor.py) says the files on disk still don't match what's loaded.
It does nothing on any night where a restart already happened, by hand or
otherwise — the sensor is already off.

## What's pruned: every auto-install automation from the old update flows

Three automations no longer belong on any client's box, because each one
installed something the moment it was merely AVAILABLE — no release notes
shown, no consent taken, nothing recorded — which is exactly what the
Updates screen (and the update_consent.py audit trail behind it) exists to
stop happening silently:

- Auto-install dashboard frontend update: installed a new dashboard-dist
  build the instant update.py's coordinator saw one.
- Force-check bridge update / Auto-install bridge update: a HACS-era pair
  that refreshed and then auto-installed HACS's own generated bridge
  update entity.

None of these are conditional on whether HACS (or the old add-on) is
still present the way an earlier version of this file treated them —
every client gets all three removed unconditionally now, because the
replacement isn't "a different auto-installer", it's the Updates screen
itself: the bridge downloads and installs a specific release directly
(frontend_updater.py / bridge_updater.py) only once a household member has
actually seen the release notes and ticked the consent box.

Why automations.yaml rather than something this integration just does in
Python directly: a client should be able to see, disable, or delete any of
these like any other automation — not have it be invisible behavior baked
into the integration. automations.yaml is a plain, user-facing YAML file
(not `.storage/`, which is Home Assistant's internal state) — the same file
the Automation Editor UI itself reads and writes; this only differs in doing
it once, programmatically, on first setup (for the one still seeded) or on
every setup (for pruning — a client who re-adds one of these three by hand
is assumed to know what they're doing, but that's an edge case none of our
clients have hit, so this doesn't specifically special-case it).

Assumes the default HAOS onboarding layout (`automation: !include
automations.yaml` in configuration.yaml). A client whose configuration.yaml
doesn't include automations.yaml that way won't have the restart automation
seeded — same as any other YAML-file automation would need adding by hand
in that case.
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
# on every future setup, and the fixed ids every pruned automation was
# seeded under. binary_sensor.py's entity has a deterministic, device-less
# slug (it's the only entity of its kind on a single-instance-per-client
# config entry), so hardcoding it here is safe.
RESTART_REQUIRED_SENSOR = "binary_sensor.bridge_restart_required"

RESTART_AUTOMATION_ID = "everrise_dashboard_restart_if_pending"

FORCE_CHECK_BRIDGE_AUTOMATION_ID = "everrise_dashboard_force_check_bridge_update"
AUTO_INSTALL_AUTOMATION_ID = "everrise_dashboard_auto_install_bridge_update"
AUTO_INSTALL_FRONTEND_AUTOMATION_ID = "everrise_dashboard_auto_install_frontend_update"

# Every automation an earlier version of this file used to seed for a
# now-retired auto-install flow — see the module docstring. Pruned
# unconditionally, on every client, regardless of whether HACS or the old
# add-on happens to still be present.
PRUNED_AUTOMATION_IDS = frozenset(
    {FORCE_CHECK_BRIDGE_AUTOMATION_ID, AUTO_INSTALL_AUTOMATION_ID, AUTO_INSTALL_FRONTEND_AUTOMATION_ID}
)

SEEDED_AUTOMATIONS = [
    {
        "id": RESTART_AUTOMATION_ID,
        "alias": "Restart HA for pending bridge update",
        "description": (
            "Only restarts if a backend update was installed from the dashboard's Updates screen "
            "but Home Assistant hasn't been restarted to load it yet (see the bridge's \"Bridge "
            "restart required\" binary sensor) — if this client already restarted (from the Updates "
            "screen's own prompt, or manually) that sensor is already off and this does nothing at "
            "3am. Seeded automatically by the bridge on first setup; edit or delete it like any "
            "other automation — it's never re-created once it exists."
        ),
        "triggers": [{"trigger": "time", "at": "03:00:00"}],
        "conditions": [{"condition": "state", "entity_id": RESTART_REQUIRED_SENSOR, "state": "on"}],
        "actions": [{"action": "homeassistant.restart"}],
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
    id, so a client who deleted it still only gets that one re-seeded —
    never a duplicate of one they kept, and a pruned id is never re-added
    even if it happens to also appear in SEEDED_AUTOMATIONS (it doesn't
    today; kept as a deliberate ordering guarantee, not an incidental one).
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
            "Removed %d automation(s) from a retired auto-install flow (%s) from %s",
            len(pruned),
            ", ".join(pruned),
            automations_path,
        )
    return True


async def async_sync_seeded_automations(hass: HomeAssistant) -> None:
    automations_path = Path(hass.config.path("automations.yaml"))

    changed = await hass.async_add_executor_job(_sync, automations_path, PRUNED_AUTOMATION_IDS)
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
