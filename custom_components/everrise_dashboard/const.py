"""Constants for the Everrise Dashboard Config Bridge integration."""

DOMAIN = "everrise_dashboard"

CONF_FOLDER = "folder"
CONF_FILENAME = "filename"

DEFAULT_FOLDER = "everrise-dashboard"
DEFAULT_FILENAME = "config.json"

# A single path segment only — no "/", "..", or drive letters. Enforced both
# at config_flow submission time and again at request time (defense in
# depth), since this string becomes part of a filesystem path.
FOLDER_PATTERN = r"^[A-Za-z0-9_-]+$"
FILENAME_PATTERN = r"^[A-Za-z0-9_.-]+\.json$"

# Where the compiled dashboard frontend lives — a fixed name, not derived
# from CONF_FOLDER above (that option is for config.json's own subfolder,
# a separate concern). Must match __init__.py's PANEL_JS_URL
# ("/local/everrise-dashboard/..."), which hardcodes the same folder name.
WWW_SUBFOLDER = "everrise-dashboard"

# Source of truth for the compiled frontend — see frontend_updater.py.
# Public repo (no auth token needed for a client's HA instance to pull from
# it), fetched over plain HTTPS rather than `git`: Home Assistant OS's core
# container doesn't ship a git binary.
DIST_REPO_OWNER = "EverRiseDeveloper"
DIST_REPO_NAME = "dashboard-dist"
DIST_REPO_BRANCH = "main"
DIST_VERSION_URL = (
    f"https://raw.githubusercontent.com/{DIST_REPO_OWNER}/{DIST_REPO_NAME}/{DIST_REPO_BRANCH}/version.json"
)
DIST_TARBALL_URL = (
    f"https://codeload.github.com/{DIST_REPO_OWNER}/{DIST_REPO_NAME}/tar.gz/refs/heads/{DIST_REPO_BRANCH}"
)
