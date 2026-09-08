"""What a NON-ADMIN client is allowed to make Home Assistant do.

This is the security boundary for the dashboard's automation builder, and
the reason it exists is worth stating plainly.

Home Assistant's own automation config endpoints
(`/api/config/automation/config/*`) are admin-only, with no user policy
that can grant a non-admin account access to them. Every EverRise client is
a non-admin user, so the builder's create/edit/delete path returned 401 for
all of them — visible in the HA log as repeated "request with invalid
authentication" entries against that URL, which `http.ban` also counts as
failed login attempts.

The fix is for this integration to perform the write instead: it runs
in-process with full privileges, so it can serve a non-admin caller without
that caller ever touching an admin endpoint. That trades an admin-only
endpoint for a narrow, scoped one — better than the alternative of making
clients admin, which on a Supervised install also hands them a root shell
via the SSH add-on and read access to every secret on the box.

But it only holds if the payload is constrained. An automation can call
`shell_command.*`, `python_script.*` or `hassio.addon_restart`, so a naive
proxy would convert "the client may build automations" into "the client may
run arbitrary code on their own HA host" — and, since the same code ships
to every install, into a uniform escalation path across the whole fleet.
Hence an allowlist, enforced HERE rather than only in the frontend: the
frontend's validation is a usability feature and can be bypassed by anyone
willing to use curl with their own token.

The allowlist is deliberately expressed as what the builder can EMIT (see
the dashboard repo's src/lib/automations.ts). If a new event type or action
is added there, the corresponding entry has to be added here too, and a
rejected save is the intended failure mode — a permissive default here
would be a silent hole.
"""

from __future__ import annotations

from typing import Any

# Client-built automations all carry this id prefix; it is what separates
# them from anything hand-authored in HA (see AUTOMATION_ID_PREFIX in the
# dashboard's automations.ts).
CLIENT_ID_PREFIX = "everrise_"

# ...except this narrower prefix, which is THIS integration's own seeded
# update/restart automations. They share the outer prefix, so without an
# explicit carve-out a client could edit or delete the machinery that keeps
# their dashboard updated. Must stay in step with restart_automation.py.
RESERVED_ID_PREFIX = "everrise_dashboard_"

# Purpose-specific entity triggers plus the classic platforms the builder
# falls back to. Anything else — notably `event`, `webhook`, `template`,
# `mqtt` and `time_pattern` — is refused: each is either a way to smuggle in
# templating the builder never produces, or simply something the UI cannot
# create, so its presence means the payload did not come from the builder.
ALLOWED_TRIGGERS = frozenset(
    {
        "door.opened",
        "door.closed",
        "garage_door.opened",
        "garage_door.closed",
        "lock.locked",
        "lock.unlocked",
        "motion.detected",
        "motion.cleared",
        "occupancy.detected",
        "occupancy.cleared",
        "state",
        "numeric_state",
        "time",
        "sun",
        "zone",
    }
)

ALLOWED_CONDITIONS = frozenset({"time", "state", "numeric_state", "zone", "sun"})

# Exact service names, never prefixes. A domain-level wildcard would be the
# obvious shortcut and the obvious mistake: `hassio.*` and `homeassistant.*`
# both contain host-level operations.
ALLOWED_ACTION_SERVICES = frozenset(
    {
        "light.turn_on",
        "light.turn_off",
        "fan.turn_on",
        "fan.turn_off",
        "lock.lock",
        "lock.unlock",
        "alarm_control_panel.alarm_arm_away",
        "alarm_control_panel.alarm_arm_home",
        "alarm_control_panel.alarm_arm_night",
        "alarm_control_panel.alarm_disarm",
        "media_player.media_play",
        "media_player.media_pause",
        "media_player.media_stop",
        "media_player.media_next_track",
        "media_player.media_previous_track",
        "media_player.volume_set",
        "notify.send_message",
        "tts.speak",
        "camera.snapshot",
    }
)

# `notify.<per-device service>` is the one place a prefix match is required,
# because the service name is whatever the Companion App registered for that
# phone (notify.mobile_app_<slug>). Restricted to the notify domain, which
# only ever sends messages.
ALLOWED_ACTION_SERVICE_PREFIXES = ("notify.",)

ALLOWED_MODES = frozenset({"single", "restart"})

# camera.snapshot writes a file, so its destination is confined. Home
# Assistant already checks the path against allowlist_external_dirs, but
# that permits the whole of /config on many installs — this narrows it to
# the folder the dashboard serves its own event snapshots from.
SNAPSHOT_DIR_PREFIX = "/config/www/everrise-dashboard/events/"

# The one automation-level variable the builder emits (the shared timestamp
# behind a snapshot's filename and the notification's image URL).
ALLOWED_VARIABLE_NAMES = frozenset({"everrise_snap_ts"})

# Top-level keys the builder produces. An unexpected key is refused rather
# than ignored, so a payload carrying something this policy has never
# reasoned about cannot be written.
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {"id", "alias", "description", "triggers", "conditions", "actions", "mode", "variables"}
)


def _service_allowed(service: str) -> bool:
    if service in ALLOWED_ACTION_SERVICES:
        return True
    return any(service.startswith(prefix) for prefix in ALLOWED_ACTION_SERVICE_PREFIXES)


def is_client_owned_id(automation_id: Any) -> bool:
    """Whether a non-admin caller may create/edit/delete this id at all."""
    return (
        isinstance(automation_id, str)
        and automation_id.startswith(CLIENT_ID_PREFIX)
        and not automation_id.startswith(RESERVED_ID_PREFIX)
    )


def _check_action(action: Any, index: int, errors: list[str]) -> None:
    where = f"actions[{index}]"
    if not isinstance(action, dict):
        errors.append(f"{where} is not an object")
        return

    # A delay block carries no `action` key at all.
    if "delay" in action and "action" not in action:
        extra = set(action) - {"delay"}
        if extra:
            errors.append(f"{where} mixes a delay with unexpected keys: {sorted(extra)}")
        return

    service = action.get("action")
    if not isinstance(service, str):
        errors.append(f"{where} has no service name")
        return
    if not _service_allowed(service):
        errors.append(f"{where} calls '{service}', which clients may not call")
        return

    if service == "camera.snapshot":
        filename = (action.get("data") or {}).get("filename")
        if not isinstance(filename, str) or not filename.startswith(SNAPSHOT_DIR_PREFIX):
            errors.append(f"{where}: snapshots may only be written under {SNAPSHOT_DIR_PREFIX}")


def validate_client_automation(config: Any) -> list[str]:
    """Return a list of reasons this payload is not acceptable from a
    non-admin client. An empty list means it may be written."""
    errors: list[str] = []

    if not isinstance(config, dict):
        return ["Automation must be a JSON object"]

    unexpected = set(config) - ALLOWED_TOP_LEVEL_KEYS
    if unexpected:
        errors.append(f"Unexpected keys: {sorted(unexpected)}")

    if not is_client_owned_id(config.get("id")):
        errors.append(
            f"id must start with '{CLIENT_ID_PREFIX}' and must not start with '{RESERVED_ID_PREFIX}'"
        )

    alias = config.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        errors.append("alias (the automation's name) must be a non-empty string")

    mode = config.get("mode", "single")
    if mode not in ALLOWED_MODES:
        errors.append(f"mode '{mode}' is not allowed (expected one of {sorted(ALLOWED_MODES)})")

    variables = config.get("variables", {})
    if not isinstance(variables, dict):
        errors.append("variables must be an object")
    else:
        unknown_vars = set(variables) - ALLOWED_VARIABLE_NAMES
        if unknown_vars:
            errors.append(f"Unexpected variables: {sorted(unknown_vars)}")

    triggers = config.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        errors.append("triggers must be a non-empty list")
    else:
        for i, trigger in enumerate(triggers):
            if not isinstance(trigger, dict):
                errors.append(f"triggers[{i}] is not an object")
                continue
            kind = trigger.get("trigger")
            if kind not in ALLOWED_TRIGGERS:
                errors.append(f"triggers[{i}] uses '{kind}', which clients may not use")

    conditions = config.get("conditions", [])
    if not isinstance(conditions, list):
        errors.append("conditions must be a list")
    else:
        for i, condition in enumerate(conditions):
            if not isinstance(condition, dict):
                errors.append(f"conditions[{i}] is not an object")
                continue
            kind = condition.get("condition")
            if kind not in ALLOWED_CONDITIONS:
                errors.append(f"conditions[{i}] uses '{kind}', which clients may not use")

    actions = config.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions must be a non-empty list")
    else:
        for i, action in enumerate(actions):
            _check_action(action, i, errors)

    return errors
