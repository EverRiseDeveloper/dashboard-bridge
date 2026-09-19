"""Fetches and installs a specific tagged release of this integration's OWN
source (dashboard-bridge) — the backend half of the Updates screen's
install endpoint (update_manager.py).

Mirrors frontend_updater.py's approach and reasoning (no git binary on
Home Assistant OS, so a plain HTTPS tarball fetch plus stdlib tarfile
extraction, with an atomic swap so a client never sees a half-updated
integration on disk if this is interrupted partway through) with one
structural difference: dashboard-bridge's tarball is the WHOLE repo, not
just the integration folder, so the extracted tree has to be descended
into custom_components/<domain>/ before it can be swapped into place.

This deliberately always installs an EXPLICIT version — the release a
customer consented to via the Updates screen (see update_http.py's accept
endpoint) — never "whatever's on main". There's no bootstrap use case for
the bridge's own code the way there is for the frontend (the bridge is
already running by the time any of this code executes), so there's no
optional-version bare-main case to support here at all.

Swapping the currently running integration's own directory while this same
process is still executing code imported from it is safe: Python doesn't
re-read a module's source file after import, it only ever runs the
bytecode already loaded into memory. Home Assistant requires an explicit
restart to pick up the new files either way — see binary_sensor.py, which
detects that exact gap and flags it for the Updates screen.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tarfile
from pathlib import Path
from uuid import uuid4

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import BRIDGE_REPO_NAME, BRIDGE_REPO_OWNER, DOMAIN

_LOGGER = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


def _tarball_url_for_version(version: str) -> str:
    return f"https://codeload.github.com/{BRIDGE_REPO_OWNER}/{BRIDGE_REPO_NAME}/tar.gz/refs/tags/{version}"


def _install_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("custom_components", DOMAIN))


def _extract_tarball(archive_path: Path, target: Path) -> None:
    """Extract dashboard-bridge's tarball (which wraps the whole repo in a
    single <repo>-<tag>/ top-level directory) and atomically swap this
    integration's own on-disk folder for the custom_components/<domain>/
    subtree found inside it.

    Same atomic rename pattern as frontend_updater.py's _extract_tarball:
    build the new content aside, rename the old folder out of the way,
    rename the new one into place, then clean up — so a client never sees
    a half-written integration on disk even if this is interrupted."""
    extract_root = archive_path.parent / f"extract-{uuid4().hex}"
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path) as tar:
        tar.extractall(extract_root, filter="data")

    inner_dirs = [p for p in extract_root.iterdir() if p.is_dir()]
    if len(inner_dirs) != 1:
        raise RuntimeError(f"Unexpected dashboard-bridge tarball layout under {extract_root}")
    repo_root = inner_dirs[0]

    new_content = repo_root / "custom_components" / DOMAIN
    if not new_content.is_dir():
        raise RuntimeError(
            f"dashboard-bridge tarball had no custom_components/{DOMAIN}/ folder under {repo_root}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.new-{uuid4().hex}")
    shutil.move(str(new_content), str(staging))

    had_previous = target.exists()
    old = target.with_name(f"{target.name}.old-{uuid4().hex}")
    if had_previous:
        target.rename(old)
    staging.rename(target)
    if had_previous:
        shutil.rmtree(old, ignore_errors=True)

    shutil.rmtree(extract_root, ignore_errors=True)


async def install_bridge_version(hass: HomeAssistant, version: str) -> bool:
    """Download this exact tagged release of dashboard-bridge and swap it
    into custom_components/<domain>/ in place. Returns True on success.

    Always restart-gated afterward — see binary_sensor.py, which picks up
    the mismatch on its own within a few minutes, and update_manager.py's
    install_updates, which nudges it to refresh immediately."""
    session = async_get_clientsession(hass)
    tmp_dir = Path(hass.config.path(".storage")) / f"everrise_dashboard_bridge_dl_{uuid4().hex}"
    archive_path = tmp_dir / "dashboard-bridge.tar.gz"
    tarball_url = _tarball_url_for_version(version)

    def _make_tmp_dir() -> None:
        tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        await hass.async_add_executor_job(_make_tmp_dir)
    except OSError as err:
        _LOGGER.error("Couldn't create a temp download dir at %s: %s", tmp_dir, err)
        return False

    try:
        async with session.get(tarball_url, timeout=_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                _LOGGER.error("Downloading dashboard-bridge %s failed: HTTP %s", version, resp.status)
                return False
            data = await resp.read()

        def _write_and_extract() -> None:
            archive_path.write_bytes(data)
            _extract_tarball(archive_path, _install_dir(hass))

        await hass.async_add_executor_job(_write_and_extract)
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, tarfile.TarError) as err:
        _LOGGER.error("Failed installing dashboard-bridge %s: %s", version, err)
        return False
    finally:
        await hass.async_add_executor_job(shutil.rmtree, tmp_dir, True)
