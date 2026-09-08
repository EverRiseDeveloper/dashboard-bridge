"""Reading and writing Home Assistant's own automations.yaml.

Deliberately the SAME file Home Assistant's built-in automation editor
uses (`config/automations.yaml`, the target of the default
`automation: !include automations.yaml`), and via the same
`homeassistant.util.yaml` load/dump helpers, so an automation written here
is indistinguishable from one written in Settings -> Automations. That
matters: the client's automations keep working with HA's own traces, the
debugger, and the automation editor, and nothing has to be migrated if this
integration is ever removed.

Writes are atomic (temp file in the same directory, then os.replace) and
preserve every entry this integration did not create — including the
installer's own hand-written automations, which sit in the same list.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import dump as yaml_dump, load_yaml

_LOGGER = logging.getLogger(__name__)

AUTOMATIONS_FILENAME = "automations.yaml"


def automations_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(AUTOMATIONS_FILENAME))


def load_automations(path: Path) -> list[dict[str, Any]]:
    """The whole file as a list. A missing or empty file is an empty list,
    not an error — a fresh install legitimately has neither."""
    if not path.exists():
        return []
    loaded = load_yaml(str(path))
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        # A dict here means someone has restructured automations.yaml by
        # hand into a form HA's own editor also cannot write. Refusing is
        # safer than guessing and flattening their file.
        raise ValueError(f"{path} does not contain a list of automations")
    return [item for item in loaded if isinstance(item, dict)]


def write_automations_atomic(path: Path, automations: list[dict[str, Any]]) -> None:
    # Dump BEFORE touching the filesystem: if serialisation raises, the
    # existing file is still intact (the same ordering HA's own config view
    # uses, and for the same reason).
    contents = yaml_dump(automations)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    tmp.write_text(contents, encoding="utf-8")
    os.replace(tmp, path)


def upsert(automations: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace the entry with a matching id, or append if there isn't one.
    Order is preserved so an edit doesn't reshuffle the client's list."""
    target_id = config.get("id")
    for index, existing in enumerate(automations):
        if existing.get("id") == target_id:
            return [*automations[:index], config, *automations[index + 1 :]]
    return [*automations, config]


def remove(automations: list[dict[str, Any]], automation_id: str) -> tuple[list[dict[str, Any]], bool]:
    kept = [a for a in automations if a.get("id") != automation_id]
    return kept, len(kept) != len(automations)
