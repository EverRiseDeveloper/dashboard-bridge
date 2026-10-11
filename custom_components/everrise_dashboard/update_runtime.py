"""In-memory state the update flow shares between its pieces, for as long as
this Home Assistant process runs:

- the lock that allows ONE install at a time per house (update_manager.py),
  and who is installing right now;
- when a restart was last asked for, so two people saying "yes, restart"
  restart Home Assistant once (update_http.py);
- the latest result of the read-only background check (update_notifier.py).

The status sensor (sensor.py) listens here, so every open dashboard sees
"available" / "installing" / "up to date" change live without each phone
asking GitHub itself.

Kept in hass.data rather than on the config entry: reloading the entry must
not hand out a second, unlocked lock while an install is still running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_RUNTIME_KEY = "update_runtime"

STATUS_UP_TO_DATE = "up_to_date"
STATUS_AVAILABLE = "available"
STATUS_INSTALLING = "installing"
STATUS_UNKNOWN = "unknown"
STATUS_OPTIONS = [STATUS_UP_TO_DATE, STATUS_AVAILABLE, STATUS_INSTALLING, STATUS_UNKNOWN]


@dataclass
class UpdateRuntime:
    install_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Who started the install that holds install_lock, and when:
    # {"userId", "userName", "startedAt", "backendVersion", "frontendVersion"}.
    installing: dict[str, Any] | None = None
    # time.monotonic() of the last accepted restart request.
    restart_requested_at: float | None = None
    # Latest SUCCESSFUL check, without release notes:
    # {"backend": {"installed", "latest", "available"}, "frontend": {...}}.
    last_check: dict[str, Any] | None = None
    last_checked_at: str | None = None
    last_check_error: str | None = None
    _listeners: list[Callable[[], None]] = field(default_factory=list)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def async_notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def status(self) -> str:
        if self.installing is not None:
            return STATUS_INSTALLING
        if self.last_check is None:
            return STATUS_UNKNOWN
        if any(self.last_check.get(side, {}).get("available") for side in ("backend", "frontend")):
            return STATUS_AVAILABLE
        return STATUS_UP_TO_DATE

    def attributes(self) -> dict[str, Any]:
        check = self.last_check or {}
        backend = check.get("backend") or {}
        frontend = check.get("frontend") or {}
        installing = self.installing or {}
        return {
            "frontend_installed": frontend.get("installed"),
            "frontend_latest": frontend.get("latest"),
            "frontend_available": bool(frontend.get("available")),
            "backend_installed": backend.get("installed"),
            "backend_latest": backend.get("latest"),
            "backend_available": bool(backend.get("available")),
            "installing_by": installing.get("userName"),
            "installing_since": installing.get("startedAt"),
            "last_checked": self.last_checked_at,
            "last_check_error": self.last_check_error,
        }

    @callback
    def async_mark_installed(self, side: str, version: str) -> None:
        """After a successful install, so the badge clears straight away
        rather than at the next background check."""
        if self.last_check is None or side not in self.last_check:
            return
        self.last_check[side] = {**self.last_check[side], "installed": version, "available": False}


def get_update_runtime(hass: HomeAssistant) -> UpdateRuntime:
    domain_data = hass.data.setdefault(DOMAIN, {})
    runtime = domain_data.get(_RUNTIME_KEY)
    if runtime is None:
        runtime = UpdateRuntime()
        domain_data[_RUNTIME_KEY] = runtime
    return runtime
