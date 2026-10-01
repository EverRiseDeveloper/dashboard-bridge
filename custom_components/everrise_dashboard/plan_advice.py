"""The energy plan's advice: the home's AI (Home Assistant's AI Task) reads
the house's numbers and the weather, which the dashboard sends
(lib/adviceFacts.ts in the dashboard repo), and says what to do in the next
hour and tonight.

A Gemini Flash model is asked first; when it's busy (Google's "high demand"
503s are common) or fails, a Flash-Lite model is asked straight away
instead, and the advice says which model wrote it. Asked at most once every
ten minutes for the whole home, whichever phone or tablet asks, so every
screen shows the same advice. When no model can answer, the last good
advice stays, with when it was written, and it's asked again ten minutes
later. Kept under this integration's own storage (never in www/), so it
outlives a restart, and read through the authenticated API in
plan_advice_http.py. Nothing is sent anywhere unless the home has an AI set
up in Home Assistant.
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
from .plan_advice_words import STRUCTURE, TASK_NAME, accept_advice, instructions_of, place_of
from .storage import base_dir, read_json, write_json_atomic

_LOGGER = logging.getLogger(__name__)

# The AI is asked at most this often, and this long after an ask that failed.
GAP = timedelta(minutes=10)
# Each model gets this long, and at most this many are tried per ask: Flash,
# then Flash-Lite when Flash is busy.
AI_TIMEOUT_S = 45
MODELS_PER_ASK = 2


def _store_path(hass: HomeAssistant) -> Path:
    return base_dir(hass) / "plan" / "advice.json"


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
    """Flash first, then Flash-Lite, then any other Google model, then the rest."""
    if "flash" in words:
        return 1 if "lite" in words else 0
    return 2 if re.search("google|gemini", words) else 3


def ai_order(hass: HomeAssistant) -> list[str]:
    """The home's AIs (AI Task entities that aren't unavailable), in the order
    they're asked."""
    tasks = [
        (state.entity_id, f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower())
        for state in hass.states.async_all("ai_task")
        if state.state != "unavailable"
    ]
    return [entity_id for entity_id, words in sorted(tasks, key=lambda task: (_rank(task[1]), task[0]))]


def name_of(hass: HomeAssistant, entity_id: str) -> str:
    """The name the household gave a model in Home Assistant ("Google AI Task
    Flash"), or its entity id without one."""
    state = hass.states.get(entity_id)
    name = state.attributes.get("friendly_name") if state else None
    return name if isinstance(name, str) and name.strip() else entity_id


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


class PlanAdvice:
    """The home's one copy of the advice, and the one ask on its way."""

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

    def _view(self, record: dict[str, Any]) -> dict[str, Any]:
        last = _time(record.get("lastTry"))
        model = record.get("model")
        return {
            "ai": bool(ai_order(self._hass)),
            "advice": record.get("advice"),
            "at": record.get("at"),
            # Which model wrote it; advice kept from before the name was
            # saved with it goes by the model's name now.
            "model": record.get("modelName") or (name_of(self._hass, model) if isinstance(model, str) else None),
            "busy": bool(record.get("busy")),
            "updating": self._asking(),
            "nextTry": (last + GAP).isoformat() if last else None,
        }

    async def view(self) -> dict[str, Any]:
        """Where the advice stands."""
        return self._view(await self._load())

    async def ask(self, facts: str) -> dict[str, Any]:
        """Ask the AI with these numbers if it's due, or wait for the ask
        already on its way, then say where the advice stands."""
        record = await self._load()
        if not self._asking():
            last = _time(record.get("lastTry"))
            due = last is None or dt_util.utcnow() - last >= GAP
            if due and ai_order(self._hass):
                self._task = self._hass.async_create_task(self._ask(record, facts), "EverRise plan advice")
        if self._task is not None and not self._task.done():
            try:
                async with asyncio.timeout(AI_TIMEOUT_S * MODELS_PER_ASK + 5):
                    await asyncio.shield(self._task)
            except TimeoutError:
                pass
        return self._view(record)

    async def _ask_one(self, entity_id: str, instructions: str) -> tuple[dict[str, str] | None, str]:
        """One model's advice, or None and why not."""
        try:
            async with asyncio.timeout(AI_TIMEOUT_S):
                response = await self._hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {"entity_id": entity_id, "task_name": TASK_NAME, "instructions": instructions, "structure": STRUCTURE},
                    blocking=True,
                    return_response=True,
                )
        except Exception as err:  # noqa: BLE001 - busy, out of quota or unreachable: the next model is asked
            return None, str(err) or type(err).__name__
        advice = accept_advice(response.get("data") if isinstance(response, dict) else None)
        return advice, "" if advice else "its answer didn't have the advice in it"

    async def _ask(self, record: dict[str, Any], facts: str) -> None:
        record["lastTry"] = dt_util.utcnow().isoformat()
        instructions = instructions_of(facts, place_of(self._hass.config.time_zone))
        problems: list[str] = []
        for entity_id in ai_order(self._hass)[:MODELS_PER_ASK]:
            advice, problem = await self._ask_one(entity_id, instructions)
            if advice:
                record.update(
                    advice=advice,
                    at=dt_util.utcnow().isoformat(),
                    busy=False,
                    model=entity_id,
                    modelName=name_of(self._hass, entity_id),
                )
                break
            problems.append(f"{entity_id}: {problem}")
        else:
            record["busy"] = True
            _LOGGER.warning(
                "Plan advice: the home's AI couldn't answer (%s), so it's asked again in 10 minutes",
                "; ".join(problems) or "there's no AI Task in Home Assistant",
            )
        try:
            await self._hass.async_add_executor_job(write_json_atomic, _store_path(self._hass), dict(record))
        except OSError as err:
            _LOGGER.error("Couldn't save the plan's advice: %s", err)


def advice_of(hass: HomeAssistant) -> PlanAdvice:
    """The one PlanAdvice for this Home Assistant."""
    data = hass.data.setdefault(DOMAIN, {})
    if "plan_advice" not in data:
        data["plan_advice"] = PlanAdvice(hass)
    return data["plan_advice"]
