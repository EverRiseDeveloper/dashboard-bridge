"""The Everrise Dashboard Config Bridge integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_FILENAME, CONF_FOLDER, DEFAULT_FILENAME, DEFAULT_FOLDER, DOMAIN
from .frontend_updater import install_latest, www_dir
from .http import DashboardConfigView
from .restart_automation import async_seed_restart_automation_if_missing
from .storage import resolve_config_path, write_json_atomic

PLATFORMS: list[Platform] = [Platform.UPDATE, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)

# Registers the dashboard as a native Home Assistant panel — a custom
# element Home Assistant mounts directly into its own already-authenticated
# page, instead of the old `iframe` Lovelace card approach. That matters
# because an iframe is a genuinely separate, unauthenticated browsing
# context (it has to do its own OAuth login dance to get a token), which
# the Companion App's WebView turned out not to support reliably in several
# unrelated ways. A native panel has no separate context to authenticate:
# Home Assistant hands it the current user's already-live `hass.connection`
# / `hass.auth` directly (see the frontend's src/panel.tsx).
#
# [FIXED — see STATIC_URL_PREFIX below] This used to be served at
# `/local/everrise-dashboard/panel.js`, piggybacking on the `frontend`
# component's own generic `/local` -> `config/www` mapping. That mapping is
# only registered by `frontend`'s *own* async_setup, and only if the `www/`
# folder already exists AT THAT MOMENT in the boot sequence
# (developers.home-assistant.io / community threads on "files added to
# www/ after startup 404 until a restart" — this is a well-known HA
# behavior, not specific to this integration). On a brand new client box,
# `www/everrise-dashboard/` doesn't exist yet the first time this
# integration's own async_setup_entry runs (which is what
# _bootstrap_frontend_if_missing below uses to create it and download the
# build) — so `/local/...` was never wired up for it during that same boot,
# and every fresh install hit a genuine, reproducible 404 on panel.js /
# version.json even though the files were sitting right there on disk
# (confirmed against a real client box: files present over Samba, 404 from
# curl on both `panel.js` and `version.json`, restarting Core fixed it).
#
# The real fix is to stop depending on `frontend`'s lazy, boot-order-
# sensitive mapping entirely: register our own static path explicitly,
# every time this integration sets up (see the async_register_static_paths
# call below). That's a dynamic aiohttp route registration, not gated on
# boot order the way `frontend`'s is — it works whether www/ was just
# created a second ago or has existed for months, and needs no restart.
STATIC_URL_PREFIX = "/everrise_dashboard_static"
PANEL_JS_URL = f"{STATIC_URL_PREFIX}/panel.js"
PANEL_WEBCOMPONENT_NAME = "everrise-dashboard-panel"
PANEL_URL_PATH = "everrise"

# Bundled alongside this file — ships inside custom_components/everrise_dashboard/
# with every HACS install, same as manifest.json or the translations folder.
_BUNDLED_DEFAULT_CONFIG_PATH = Path(__file__).parent / "default_config.json"


def _seed_default_config_if_missing(hass: HomeAssistant, folder: str, filename: str) -> None:
    """Write the bundled placeholder config the first time this integration
    is set up for a client, so the dashboard has something valid to render —
    a household name to rename and the weather-derived header stats, which
    point at `weather.forecast_home`, the entity HA's own onboarding creates
    by default on every fresh install — instead of the Config Bridge API
    404ing until someone performs a first save through the Admin UI.

    Runs on every setup/reload, but only ever writes once: an existing file
    (a client's real, already-populated config) is never touched.
    """
    path = resolve_config_path(hass, folder, filename)
    if path is None or path.exists():
        return
    try:
        default_config = json.loads(_BUNDLED_DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        write_json_atomic(path, default_config)
        _LOGGER.info("Seeded a placeholder dashboard config at %s", path)
    except OSError as err:
        _LOGGER.error("Failed seeding default config at %s: %s", path, err)


async def _bootstrap_frontend_if_missing(hass: HomeAssistant) -> None:
    """Installing the bridge alone should produce a working dashboard, not
    a 404 in www/ — so the very first setup downloads dashboard-dist the
    same way the Update entity's Install button later does (see
    frontend_updater.py). An existing install (any prior build already
    sitting in www/) is left alone; this only ever fires once per client."""
    target = www_dir(hass)
    already_installed = await hass.async_add_executor_job(lambda: target.exists() and any(target.iterdir()))
    if already_installed:
        return
    _LOGGER.info("No dashboard frontend found at %s — installing the latest build", target)
    if not await install_latest(hass):
        _LOGGER.error(
            "Couldn't install the dashboard frontend automatically. The bridge/config API will still "
            "work, but the dashboard itself won't load until this is retried (reload the integration, "
            "or wait for the Update entity's next check) or installed manually."
        )


async def _register_static_path_if_missing(hass: HomeAssistant) -> None:
    """See the long comment on STATIC_URL_PREFIX above for why this exists
    instead of just relying on `frontend`'s `/local` mapping. Registering
    this is cheap and idempotent-guarded the same way as the HTTP view and
    panel below; the target directory is created up front (even if empty)
    because aiohttp's static route needs the path to exist at registration
    time — _bootstrap_frontend_if_missing (or the Update entity later)
    fills it with real files afterward, and this route serves whatever's
    there on each request, not a snapshot taken at registration."""
    if hass.data[DOMAIN].get("static_path_registered"):
        return
    target = www_dir(hass)
    await hass.async_add_executor_job(lambda: target.mkdir(parents=True, exist_ok=True))
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_PREFIX, str(target), False)]
    )
    hass.data[DOMAIN]["static_path_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    folder = entry.options.get(CONF_FOLDER, DEFAULT_FOLDER)
    filename = entry.options.get(CONF_FILENAME, DEFAULT_FILENAME)
    await hass.async_add_executor_job(_seed_default_config_if_missing, hass, folder, filename)
    await _register_static_path_if_missing(hass)
    await _bootstrap_frontend_if_missing(hass)
    await async_seed_restart_automation_if_missing(hass)

    # The HTTP view is registered once for the lifetime of this HA process —
    # aiohttp's router has no public "unregister route" API, so re-adding or
    # reloading the config entry must not try to register it twice. The view
    # itself looks up the live config entry (folder/filename options) fresh
    # on every request, so it stays correct across entry reloads/edits even
    # though the route registration itself is a one-time thing.
    if not hass.data[DOMAIN].get("view_registered"):
        hass.http.register_view(DashboardConfigView(hass))
        hass.data[DOMAIN]["view_registered"] = True

    # Same one-time-per-process reasoning as the HTTP view above — panel
    # registration has no "reload"/"update" story either (it's a straight
    # call into the frontend's built-in panel table), so a config entry
    # reload must not try to register the same frontend_url_path twice.
    if not hass.data[DOMAIN].get("panel_registered"):
        try:
            await async_register_panel(
                hass,
                frontend_url_path=PANEL_URL_PATH,
                webcomponent_name=PANEL_WEBCOMPONENT_NAME,
                sidebar_title="EverRise",
                sidebar_icon="mdi:home-lightning-bolt",
                module_url=PANEL_JS_URL,
                require_admin=False,
                embed_iframe=False,
                trust_external=False,
            )
        except ValueError:
            # Home Assistant raises this if frontend_url_path is already
            # registered (e.g. a second config entry reload within the same
            # HA process, since there's no "unregister" to undo the first
            # one) — not an error worth surfacing, the panel is already up.
            _LOGGER.debug("Panel %s was already registered", PANEL_URL_PATH)
        hass.data[DOMAIN]["panel_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The HTTP view, static path, and panel registration all stay in place
    # regardless (see notes above — there's no "unregister" for any of
    # them), but the Update entity platform does need a normal unload.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Options (folder/filename) are read fresh per-request by the view, so
    # no explicit reload action is needed here — this listener exists so
    # HA doesn't warn about an options flow with no update handling.
    return None
