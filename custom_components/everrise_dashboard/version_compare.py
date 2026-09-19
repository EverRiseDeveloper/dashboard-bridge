"""Plain three-part semver comparison, shared by update.py's background
coordinator (HA's native Updates page) and update_manager.py's live
check-for-updates flow (the EverRise dashboard's own Updates screen).

Kept deliberately minimal — this integration controls both ends of every
version string it ever compares (package.json/manifest.json on the way in,
version.json/manifest.json on the way out), so there's no need to handle
pre-release or build-metadata suffixes (e.g. "1.2.0-beta.1").
"""

from __future__ import annotations


def parse_semver(value: str) -> tuple[int, int, int] | None:
    """Parses a plain "MAJOR.MINOR.PATCH" string (an optional leading "v"
    is tolerated) into a comparable tuple. Anything else (unparseable,
    wrong number of parts) returns None rather than guessing, so callers
    can fall back to a safe default."""
    text = value[1:] if value[:1] in ("v", "V") else value
    parts = text.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None
    return (major, minor, patch)


def latest_is_newer(latest: str | None, installed: str | None) -> bool:
    """Nothing installed yet still counts as "newer" so an install path
    remains available as a manual retry. Otherwise, parse both as semver
    and compare numerically — a plain string inequality would flag
    "update available" even when a *stale* read reports an OLDER release
    than what's already installed (seen in practice with dashboard-dist's
    version.json shortly after a force-push, while raw.githubusercontent.com's
    cache caught up), which showed up as a confusing "update available"
    that installed nothing new because there was nothing new to install.
    Falls back to simple inequality only if either string doesn't parse as
    semver — shouldn't happen since both ends are controlled by this
    project, but don't silently hide a real update over a malformed value."""
    if installed is None:
        return latest is not None
    if latest is None:
        return False
    latest_tuple = parse_semver(latest)
    installed_tuple = parse_semver(installed)
    if latest_tuple is not None and installed_tuple is not None:
        return latest_tuple > installed_tuple
    return latest != installed
