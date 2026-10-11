"""Fetches and installs the compiled dashboard frontend (dashboard-dist)
into Home Assistant's www/ folder.

Two call sites use this: __init__.py's first-run bootstrap (a bare bridge
install should produce a working dashboard, not a 404 in www/), and the
dashboard's own Software update screen (update_manager.py — only after a
household member has read the notes and tapped Install). Both funnel
through install_frontend_version() below so the download/extract logic
can't drift between the two, and both name an exact tagged release:
nothing here can install whatever happens to be on the main branch.

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

from .const import DIST_REPO_NAME, DIST_REPO_OWNER, WWW_SUBFOLDER

_LOGGER = logging.getLogger(__name__)

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


def _read_panel_cache_token(path: Path) -> str | None:
    """A short string that changes whenever a different build lands in www/:
    the installed version plus panel.js's modification time in
    milliseconds. The version alone isn't enough, because a build can be
    redeployed under the same version number; the time alone would work but
    is unreadable in a log. None when there's no panel.js yet."""
    try:
        mtime_ms = (path / "panel.js").stat().st_mtime_ns // 1_000_000
    except OSError:
        return None
    return f"{_read_local_version(path) or 'unknown'}-{mtime_ms}"


async def get_panel_cache_token(hass: HomeAssistant) -> str | None:
    """See panel_http.py — what makes each build's panel.js address unique."""
    return await hass.async_add_executor_job(_read_panel_cache_token, www_dir(hass))


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


def _tarball_url_for_version(version: str) -> str:
    """A SPECIFIC tagged release's tarball, never whatever's currently on
    main: the Software update screen must install exactly the release a
    customer read the notes for and consented to, not a build that landed
    after they accepted."""
    return f"https://codeload.github.com/{DIST_REPO_OWNER}/{DIST_REPO_NAME}/tar.gz/refs/tags/{version}"


async def install_frontend_version(hass: HomeAssistant, version: str) -> bool:
    """Download one tagged dashboard-dist release and swap it into www/.
    Returns True on success. The version is required — see
    _tarball_url_for_version above."""
    if not version:
        _LOGGER.error("Refusing to install the dashboard without a specific release version")
        return False
    session = async_get_clientsession(hass)
    tmp_dir = Path(hass.config.path(".storage")) / f"everrise_dashboard_dl_{uuid4().hex}"
    archive_path = tmp_dir / "dashboard-dist.tar.gz"
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
                _LOGGER.error("Downloading dashboard-dist %s failed: HTTP %s", version, resp.status)
                return False
            data = await resp.read()

        def _write_and_extract() -> None:
            archive_path.write_bytes(data)
            _extract_tarball(archive_path, www_dir(hass))

        await hass.async_add_executor_job(_write_and_extract)
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, tarfile.TarError) as err:
        _LOGGER.error("Failed installing dashboard %s: %s", version, err)
        return False
    finally:
        await hass.async_add_executor_job(shutil.rmtree, tmp_dir, True)
