"""Shared on-disk storage helpers for the Everrise Dashboard Config Bridge.

Used by both the first-run seeding step (see __init__.py, which copies the
bundled default_config.json into place the first time a client sets this
integration up) and the HTTP API (see http.py, which reads/writes the same
file at runtime) — kept in one place so path resolution and atomic writes
can't drift between the two call sites.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .const import DOMAIN


def base_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(DOMAIN))


def resolve_config_path(hass: HomeAssistant, folder: str, filename: str) -> Path | None:
    """Resolve the on-disk config path, rejecting anything that would
    escape the integration's own base directory (defense in depth — the
    folder/filename were already pattern-validated at config_flow time,
    but a request-time re-check costs nothing)."""
    base = base_dir(hass).resolve()
    candidate = (base / folder / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX and Windows; tmp is on the same filesystem
