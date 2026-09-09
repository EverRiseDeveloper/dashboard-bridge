"""Fetches and installs the compiled dashboard frontend (dashboard-dist)
into Home Assistant's www/ folder.

Two call sites use this: __init__.py's first-run bootstrap (a bare bridge
install should produce a working dashboard, not a 404 in www/), and
update.py's Update entity Install button (a client picking up a new build
later). Both funnel through install_latest() below so the download/extract
logic can't drift between the two.

Deliberately does not shell out to `git`: Home Assistant OS's core
container doesn't ship a git binary, so this downloads dashboard-dist as a
plain HTTPS tarball (a public repo — no auth token needed) and extracts it
with Python's stdlib tarfile. Works identically across HAOS, Supervised,
Container, and Core installs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tarfile
from pathlib import Path
from uuid import uuid4

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DIST_TARBALL_URL, DIST_VERSION_URL, WWW_SUBFOLDER

_LOGGER = logging.getLogger(__name__)

_VERSION_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


def www_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("www", WWW_SUBFOLDER))


def _read_local_version(path: Path) -> str | None:
    version_file = path / "version.json"
    if not version_file.exists():
        return None
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return str(version) if version is not None else None


async def get_installed_version(hass: HomeAssistant) -> str | None:
    """The version currently sitting in www/ — None if nothing's deployed
    yet (a bare bridge install, before the first bootstrap install
    completes). A semver string (e.g. "1.2.3"), matching dashboard's own
    package.json — see vite.config.ts."""
    return await hass.async_add_executor_job(_read_local_version, www_dir(hass))


async def fetch_latest_version(hass: HomeAssistant) -> tuple[str | None, str | None]:
    """The newest version published to dashboard-dist, as
    ``(version, error)`` — exactly one of the two is ever set.

    A small, single-file fetch — cheap enough to call on every periodic
    check without pulling down the whole build just to compare versions.

    The error is RETURNED rather than only logged, because logging it here
    was actively misleading in practice. This function used to swallow
    every failure at ``_LOGGER.debug`` and return ``None``, and ``None``
    means "nothing newer" to the caller — so a box that could not reach
    GitHub at all reported a confident, permanently stale "up to date",
    and the only record of why sat below the default log level. That cost
    real time twice: once chasing a stalled check on the dev box, and
    again on Client_003, where the reason turned out to be
    ``Cannot connect to host raw.githubusercontent.com:443 ssl:default
    [Network unreachable]`` — visible only after turning debug on by hand
    and waiting out a 15-minute poll. Handing the reason back lets the
    coordinator put it in the warning it already logs, and publish it as
    an entity attribute so the dashboard's own About tab can show it.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{DIST_VERSION_URL}?_={uuid4().hex}", timeout=_VERSION_FETCH_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None, f"GitHub answered HTTP {resp.status}"
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        # str(err) on an aiohttp connector error already reads well enough
        # to put in front of an installer ("Cannot connect to host ...
        # [Network unreachable]"). asyncio.TimeoutError stringifies to ""
        # though, so name it explicitly rather than surfacing a blank.
        if isinstance(err, asyncio.TimeoutError):
            reason = (
                f"Timed out after {int(_VERSION_FETCH_TIMEOUT.total or 0)}s "
                "waiting for raw.githubusercontent.com"
            )
        else:
            reason = str(err) or err.__class__.__name__
        return None, reason
    version = data.get("version") if isinstance(data, dict) else None
    if version is None:
        return None, "version.json had no \"version\" field"
    return str(version), None



def _extract_tarball(archive_path: Path, target: Path) -> None:
    """Extract GitHub's tarball (which wraps everything in a single
    `<repo>-<branch>/` top-level directory) and atomically swap it in for
    whatever was in `target` before — a client never sees a half-updated
    dashboard if this is interrupted partway through."""
    extract_root = archive_path.parent / f"extract-{uuid4().hex}"
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path) as tar:
        tar.extractall(extract_root, filter="data")

    inner_dirs = [p for p in extract_root.iterdir() if p.is_dir()]
    if len(inner_dirs) != 1:
        raise RuntimeError(f"Unexpected dashboard-dist tarball layout under {extract_root}")
    new_content = inner_dirs[0]

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


async def install_latest(hass: HomeAssistant) -> bool:
    """Download dashboard-dist's current build and swap it into www/.
    Returns True on success — used both for the first-install bootstrap
    (__init__.py) and the Update entity's Install button (update.py)."""
    session = async_get_clientsession(hass)
    tmp_dir = Path(hass.config.path(".storage")) / f"everrise_dashboard_dl_{uuid4().hex}"
    archive_path = tmp_dir / "dashboard-dist.tar.gz"

    def _make_tmp_dir() -> None:
        tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        await hass.async_add_executor_job(_make_tmp_dir)
    except OSError as err:
        _LOGGER.error("Couldn't create a temp download dir at %s: %s", tmp_dir, err)
        return False

    try:
        async with session.get(DIST_TARBALL_URL, timeout=_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                _LOGGER.error("Downloading dashboard-dist failed: HTTP %s", resp.status)
                return False
            data = await resp.read()

        def _write_and_extract() -> None:
            archive_path.write_bytes(data)
            _extract_tarball(archive_path, www_dir(hass))

        await hass.async_add_executor_job(_write_and_extract)
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, tarfile.TarError) as err:
        _LOGGER.error("Failed installing the latest dashboard build: %s", err)
        return False
    finally:
        await hass.async_add_executor_job(shutil.rmtree, tmp_dir, True)
