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
