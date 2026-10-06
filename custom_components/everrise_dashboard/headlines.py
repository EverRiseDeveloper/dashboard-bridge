"""Home's AI headlines: a fresh, light-hearted line in place of "All good.",
"All secure." and "Good night." each hour, written by the home's AI (Home
Assistant's AI Task).

A Gemini Flash-Lite model is asked when the home has one, otherwise the
home's first AI. One model per ask: when it's busy, the last good lines stay
and it's asked again in 15 minutes. Asked at most once an hour for the whole
home, and only while someone has Home on screen with the switch in Admin ->
Home left on (the dashboard asks; nothing here runs on a timer), so every
screen shows the same line. Nothing about the home is sent: see
headlines_words.py for the whole ask. Kept under this integration's own
storage, so it outlives a restart, and read through the authenticated API in
headlines_http.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .headlines_words import RECENT_KEPT, STRUCTURE, TASK_NAME, accept_headlines, instructions_of
from .plan_advice import name_of
from .storage import base_dir, read_json, write_json_atomic

_LOGGER = logging.getLogger(__name__)

# New lines at most this often, and this long after an ask that failed.
GAP = timedelta(hours=1)
GAP_AFTER_FAILURE = timedelta(minutes=15)
AI_TIMEOUT_S = 45


def _store_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / "home" / "headlines.json"


def _read(hass: HomeAssistant) -> dict[str, Any]:
    path = _store_path(hass)
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _rank(words: str) -> int:
    """Flash-Lite first, then Flash, then any other Google model, then the rest."""
    if "flash" in words:
        return 0 if "lite" in words else 1
    return 2 if re.search("google|gemini", words) else 3


def headline_ai(hass: HomeAssistant) -> str | None:
    """The AI Task entity the headlines are asked of, or None without one."""
    tasks = [
        (state.entity_id, f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower())
        for state in hass.states.async_all("ai_task")
        if state.state != "unavailable"
    ]
    ranked = sorted(tasks, key=lambda task: (_rank(task[1]), task[0]))
    return ranked[0][0] if ranked else None


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


class Headlines:
    """The home's one set of headlines, and the one ask on its way."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._record: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None

    async def _load(self) -> dict[str, Any]:
        if self._record is None:
            self._record = await self._hass.async_add_executor_job(_read, self._hass)
        return self._record

    def _asking(self) -> bool:
        return self._task is not None and not self._task.done()

    @staticmethod
    def _next_try(record: dict[str, Any]) -> datetime | None:
        last = _time(record.get("lastTry"))
        if last is None:
            return None
        return last + (GAP_AFTER_FAILURE if record.get("busy") else GAP)

    def _view(self, record: dict[str, Any]) -> dict[str, Any]:
        lines = record.get("lines")
        next_try = self._next_try(record)
        return {
            "ai": headline_ai(self._hass) is not None,
            "lines": lines if isinstance(lines, dict) else None,
            "at": record.get("at"),
            "model": record.get("modelName"),
            "busy": bool(record.get("busy")),
            "updating": self._asking(),
            "nextTry": next_try.isoformat() if next_try else None,
        }

    async def view(self) -> dict[str, Any]:
        """Where the headlines stand."""
        return self._view(await self._load())

    async def ask(self) -> dict[str, Any]:
        """Ask the AI for new lines if they're due, or wait for the ask
        already on its way, then say where the headlines stand."""
        record = await self._load()
        if not self._asking():
            next_try = self._next_try(record)
            due = next_try is None or dt_util.utcnow() >= next_try
            entity_id = headline_ai(self._hass)
            if due and entity_id:
                self._task = self._hass.async_create_task(self._ask(record, entity_id), "EverRise home headlines")
        if self._task is not None and not self._task.done():
            try:
                async with asyncio.timeout(AI_TIMEOUT_S + 5):
                    await asyncio.shield(self._task)
            except TimeoutError:
                pass
        return self._view(record)

    async def _ask(self, record: dict[str, Any], entity_id: str) -> None:
        record["lastTry"] = dt_util.utcnow().isoformat()
        recent = record.get("recent") if isinstance(record.get("recent"), list) else []
        problem = ""
        lines: dict[str, str] | None = None
        try:
            async with asyncio.timeout(AI_TIMEOUT_S):
                response = await self._hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {
                        "entity_id": entity_id,
                        "task_name": TASK_NAME,
                        "instructions": instructions_of(recent),
                        "structure": STRUCTURE,
                    },
                    blocking=True,
                    return_response=True,
                )
            lines = accept_headlines(response.get("data") if isinstance(response, dict) else None)
            problem = "" if lines else "its answer didn't have three short, kind lines in it"
        except Exception as err:  # noqa: BLE001 - busy, out of quota or unreachable: the last lines stay
            problem = str(err) or type(err).__name__
        if lines:
            record.update(
                lines=lines,
                at=dt_util.utcnow().isoformat(),
                busy=False,
                model=entity_id,
                modelName=name_of(self._hass, entity_id),
                recent=[*recent, *lines.values()][-RECENT_KEPT:],
            )
        else:
            record["busy"] = True
            _LOGGER.info("Home headlines: %s couldn't answer (%s), so it's asked again in 15 minutes", entity_id, problem)
        try:
            await self._hass.async_add_executor_job(write_json_atomic, _store_path(self._hass), dict(record))
        except OSError as err:
            _LOGGER.error("Couldn't save Home's headlines: %s", err)


def headlines_of(hass: HomeAssistant) -> Headlines:
    """The one Headlines for this Home Assistant."""
    data = hass.data.setdefault(DOMAIN, {})
    if "headlines" not in data:
        data["headlines"] = Headlines(hass)
    return data["headlines"]
