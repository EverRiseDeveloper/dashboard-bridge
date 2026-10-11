"""Prunes every automation this integration used to seed for updates. None
is seeded any more: an update is only ever installed when a household
member taps Install on the dashboard's Software update screen, and Home
Assistant only restarts for one when that person says yes to the restart
straight afterwards.

## What's pruned, on every client, every setup

- Restart HA for pending bridge update: restarted the house at 3am if a
  bridge update was waiting — nobody chose that moment. If someone says
  "Not now" to the restart, the update now simply takes effect the next
  time Home Assistant restarts, and the dashboard keeps showing "Restart
  needed" until then.
- Auto-install dashboard frontend update: installed a new dashboard-dist
  build the instant the old update entity saw one — no notes shown, no
  consent taken.
- Force-check bridge update / Auto-install bridge update: a HACS-era pair
  that refreshed and then auto-installed HACS's own bridge update entity.

They live in automations.yaml (the same file the Automation Editor reads
and writes), so this edits that file once per setup and only if one of
them is actually there. Assumes the default HAOS layout (`automation:
!include automations.yaml`); a box without that simply has nothing to prune.

The seeding machinery below is kept (SEEDED_AUTOMATIONS is empty) so a
future automation can be added the same never-overwrite way.
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

# The fixed ids every pruned automation was seeded under — the same on
# every client.
RESTART_AUTOMATION_ID = "everrise_dashboard_restart_if_pending"

FORCE_CHECK_BRIDGE_AUTOMATION_ID = "everrise_dashboard_force_check_bridge_update"
AUTO_INSTALL_AUTOMATION_ID = "everrise_dashboard_auto_install_bridge_update"
AUTO_INSTALL_FRONTEND_AUTOMATION_ID = "everrise_dashboard_auto_install_frontend_update"

# Every automation an earlier version of this file seeded — see the module
# docstring. Pruned unconditionally, on every client.
PRUNED_AUTOMATION_IDS = frozenset(
    {
        RESTART_AUTOMATION_ID,
        FORCE_CHECK_BRIDGE_AUTOMATION_ID,
        AUTO_INSTALL_AUTOMATION_ID,
        AUTO_INSTALL_FRONTEND_AUTOMATION_ID,
    }
)

# Nothing is seeded today — see the module docstring.
SEEDED_AUTOMATIONS: list[dict] = []


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
            "Removed %d automation(s) from a retired auto-update flow (%s) from %s",
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
