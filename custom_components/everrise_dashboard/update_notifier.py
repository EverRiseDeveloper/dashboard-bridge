"""Tells the household when a new EverRise update is out — never installs it.

Every 3 hours (and once a few minutes after Home Assistant starts) this
runs the same read-only GitHub check as the Software update screen's own
"Check for updates" (update_manager.check_for_updates). The result feeds
the status sensor (sensor.py, via update_runtime.py), which is what puts the
"update available" badge on the Software update row.

The first time a version is seen, every phone signed in to this home's
Companion app gets one push, and the same message goes in the Message
Centre. Tapping the push opens the Software update screen. Which versions
have already been announced is kept on disk (update_notified.json), so a
restart never repeats a push; a newer version gets its own push.

Installing stays where it was: only when someone taps Install on that
screen and agrees to the T&C (update_manager.install_updates).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import PANEL_URL_PATH
from .notifications_store import put_notification
from .storage import base_dir, read_json, write_json_atomic
from .update_manager import check_for_updates
from .update_runtime import get_update_runtime

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(hours=3)
FIRST_CHECK_DELAY = timedelta(minutes=5)
RETRY_DELAY = timedelta(hours=1)

# Server-relative, like the automation builder's push links (see the
# dashboard's automations.ts): a path has no host to go stale. The page
# rides in a real query parameter as well as the hash, because the hash
# doesn't reliably survive the Companion app's handling of a tap — the
# dashboard's DeepLinkRedirect (App.tsx) reads everriseOpen.
UPDATES_TAP_URL = f"/{PANEL_URL_PATH}?everriseOpen=updates#/admin?section=updates"

# Same tag on every update push, so a newer one replaces an older one still
# sitting on the phone rather than stacking up.
_PUSH_TAG = "everrise-update"
_PUSH_TITLE = "EverRise update available"

_NOTIFIED_FILENAME = "update_notified.json"
_SIDES = ("frontend", "backend")


def _notified_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / _NOTIFIED_FILENAME


def _read_notified(hass: HomeAssistant) -> dict[str, str]:
    path = _notified_path(hass)
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {side: str(data[side]) for side in _SIDES if isinstance(data.get(side), str)}


def _write_notified(hass: HomeAssistant, notified: dict[str, str]) -> None:
    write_json_atomic(_notified_path(hass), notified)


def _summary(check: dict[str, Any]) -> dict[str, Any]:
    """What the status sensor needs — no release notes in entity attributes."""
    return {
        side: {
            "installed": check[side]["installed"],
            "latest": check[side]["latest"],
            "available": bool(check[side]["available"]),
        }
        for side in _SIDES
    }


def push_message(pending: dict[str, str]) -> str:
    """The push text. "App" is the dashboard and "system" is the bridge —
    the words a household knows them by. No release notes: the lock screen
    isn't the place, and the Software update screen shows them in full."""
    app = pending.get("frontend")
    system = pending.get("backend")
    if app and system:
        return f"A new EverRise update is ready (app {app}, system {system}). Tap to see what's new."
    if app:
        return f"EverRise app {app} is ready to install. Tap to see what's new."
    return f"An EverRise system update ({system}) is ready to install. Tap to see what's new."


def _message_id(pending: dict[str, str]) -> str:
    # Only letters, digits and . _ : - (see notifications_store's id rule).
    return "everrise-update-" + "-".join(f"{side}:{pending[side]}" for side in _SIDES if side in pending)


async def _async_push_to_every_phone(hass: HomeAssistant, message: str) -> int:
    """One push per Companion app phone/tablet signed in to this home (every
    notify.mobile_app_* service). Returns how many were sent."""
    targets = sorted(
        name for name in hass.services.async_services_for_domain("notify") if name.startswith("mobile_app_")
    )
    if not targets:
        _LOGGER.info("An EverRise update is available, but no Companion app phones are set up to tell")
        return 0
    payload = {
        "title": _PUSH_TITLE,
        "message": message,
        # The Companion apps reject tap/link keys beside title/message —
        # they go one level down (same as automations.ts). `url` is the key
        # documented for iOS, `clickAction` for Android; both carry the same
        # link.
        "data": {"url": UPDATES_TAP_URL, "clickAction": UPDATES_TAP_URL, "tag": _PUSH_TAG},
    }
    results = await asyncio.gather(
        *(hass.services.async_call("notify", target, payload, blocking=True) for target in targets),
        return_exceptions=True,
    )
    sent = 0
    for target, result in zip(targets, results):
        if isinstance(result, BaseException):
            _LOGGER.warning("Couldn't send the update notice to notify.%s: %s", target, result)
        else:
            sent += 1
    return sent


async def _async_announce_if_new(hass: HomeAssistant, check: dict[str, Any]) -> None:
    pending = {side: check[side]["latest"] for side in _SIDES if check[side]["available"]}
    if not pending:
        return
    notified = await hass.async_add_executor_job(_read_notified, hass)
    if all(notified.get(side) == version for side, version in pending.items()):
        return

    message = push_message(pending)
    sent = await _async_push_to_every_phone(hass, message)
    try:
        await hass.async_add_executor_job(
            lambda: put_notification(hass, _message_id(pending), _PUSH_TITLE, message)
        )
    except Exception as err:  # noqa: BLE001 - the push already went; the Message Centre copy is a bonus
        _LOGGER.warning("Couldn't add the update notice to the Message Centre: %s", err)

    # Remembered even when no phone was set up: the Message Centre and the
    # badge still say so, and a phone added later hears about the next one.
    await hass.async_add_executor_job(_write_notified, hass, {**notified, **pending})
    _LOGGER.info("Announced EverRise update %s to %d phone(s)", pending, sent)


async def async_check_and_announce(hass: HomeAssistant) -> bool:
    """One background check. Returns False if GitHub couldn't be reached."""
    runtime = get_update_runtime(hass)
    check = await check_for_updates(hass)
    runtime.last_checked_at = dt_util.now().isoformat()
    if not check.get("success"):
        # Keep the last good result (and its badge) rather than wiping it.
        runtime.last_check_error = check.get("error")
        runtime.async_notify()
        return False
    runtime.last_check = _summary(check)
    runtime.last_check_error = None
    runtime.async_notify()
    await _async_announce_if_new(hass, check)
    return True


async def async_setup_update_notifier(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedules the background check for as long as this entry is loaded."""
    retry_cancel: list = []

    @callback
    def _cancel_retry() -> None:
        while retry_cancel:
            retry_cancel.pop()()

    async def _run(_now=None) -> None:
        _cancel_retry()
        try:
            ok = await async_check_and_announce(hass)
        except Exception:  # noqa: BLE001 - a bad check must not kill the schedule
            _LOGGER.exception("Background EverRise update check failed")
            ok = False
        if not ok:
            retry_cancel.append(async_call_later(hass, RETRY_DELAY, _run))

    entry.async_on_unload(async_call_later(hass, FIRST_CHECK_DELAY, _run))
    entry.async_on_unload(async_track_time_interval(hass, _run, CHECK_INTERVAL))
    entry.async_on_unload(_cancel_retry)
