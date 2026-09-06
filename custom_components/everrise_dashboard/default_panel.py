"""Makes the EverRise panel the page Home Assistant opens on.

This is what puts a client into the EverRise dashboard the moment they open
the Home Assistant Companion app — on iPhone, iPad, Android phone and Android
tablet alike, plus any plain browser. All of those render the same Home
Assistant frontend, so there is exactly one setting behind all of them and
nothing to configure per device, per platform or per app.

How the frontend decides where to land (home-assistant/frontend,
src/data/panel.ts, getDefaultPanelUrlPath):

    hass.userData?.default_panel        # this user's own choice
      || hass.systemData?.default_panel # the instance-wide default
      || localStorage "defaultPanel"    # legacy, per-browser
      || "home"                         # Home Assistant's own overview

...and then it simply looks the result up in `hass.panels[...]`. That lookup
covers EVERY registered panel, not only Lovelace dashboards — so a
panel_custom panel like ours is a perfectly valid value even though Home
Assistant's own UI (Settings > Dashboards > "Set as default", and the
per-user picker under Profile) only ever *lists* Lovelace dashboards to pick
from. Writing the value here is therefore not a workaround: it is the same
field those two screens write, with a value their pickers just don't happen
to offer.

Two stores can hold it, and this module prefers the first:

  * The system store (the "instance-wide default" that Settings > Dashboards
    writes). One write covers every user on the install, including users
    created later, and a household member can still override it for
    themselves under Profile > Dashboard. Needs a Home Assistant new enough
    to have frontend system data.

  * Per-user stores, the fallback on older Home Assistant versions. Applied
    only to users who have not already chosen a landing page, so a
    deliberate choice is never overridden. Users added after setup aren't
    covered on this path — reload the integration to pick them up.

Nothing here is load-bearing: every failure is logged and swallowed. Landing
on the wrong page is a papercut, and refusing to set the integration up over
it would not be.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Frontend user/system data is a set of namespaced buckets; "core" is the one
# holding default_panel (alongside unrelated keys like showEntityIdPicker).
# Writing a bucket REPLACES it wholesale, so every write below copies the
# existing bucket and edits the single key rather than assigning a fresh
# dict — otherwise unrelated frontend preferences would be silently dropped.
FRONTEND_DATA_KEY = "core"
DEFAULT_PANEL_KEY = "default_panel"


async def async_apply_default_panel(hass: HomeAssistant, panel_url_path: str) -> None:
    """Point this install's landing page at `panel_url_path`."""
    if await _async_apply_system_default(hass, panel_url_path):
        return
    await _async_apply_per_user_default(hass, panel_url_path)


async def _async_apply_system_default(hass: HomeAssistant, panel_url_path: str) -> bool:
    """Set the instance-wide default. False means "not supported here"."""
    try:
        from homeassistant.components.frontend.storage import async_system_store
    except ImportError:
        # Home Assistant predates frontend system data — fall back to
        # writing each user's own store instead.
        _LOGGER.debug(
            "This Home Assistant has no frontend system store; falling back "
            "to a per-user default panel"
        )
        return False

    try:
        store = await async_system_store(hass)
        core = dict(store.data.get(FRONTEND_DATA_KEY) or {})
        if core.get(DEFAULT_PANEL_KEY) == panel_url_path:
            return True
        core[DEFAULT_PANEL_KEY] = panel_url_path
        await store.async_set_item(FRONTEND_DATA_KEY, core)
    except Exception:  # noqa: BLE001 - never fail setup over a landing page
        _LOGGER.exception("Could not set the instance-wide default panel")
        # The store exists, it just didn't take — don't also scribble over
        # every user's personal setting as a "fallback".
        return True

    _LOGGER.info(
        "Home Assistant will now open on the EverRise dashboard (/%s) for "
        "everyone on this install who hasn't picked their own landing page",
        panel_url_path,
    )
    return True


async def _async_apply_per_user_default(hass: HomeAssistant, panel_url_path: str) -> None:
    """Legacy path: set it individually for each existing human user."""
    try:
        from homeassistant.components.frontend.storage import async_user_store
    except ImportError:
        _LOGGER.warning(
            "Could not set the EverRise dashboard as the landing page: this "
            "Home Assistant exposes neither frontend store. Household members "
            "can still set it themselves under Profile > Dashboard"
        )
        return

    for user in await hass.auth.async_get_users():
        # Skip Home Assistant's own internal accounts (Supervisor, the
        # "Home Assistant Content" user, and friends) and deactivated ones —
        # none of them ever open a dashboard.
        if user.system_generated or not user.is_active:
            continue
        try:
            store = await async_user_store(hass, user.id)
            core = dict(store.data.get(FRONTEND_DATA_KEY) or {})
            # Respect anyone who already chose a landing page for themselves.
            # That includes a previous run of this same code, which is what
            # makes the loop idempotent across setups and reloads.
            if core.get(DEFAULT_PANEL_KEY):
                continue
            core[DEFAULT_PANEL_KEY] = panel_url_path
            await store.async_set_item(FRONTEND_DATA_KEY, core)
            _LOGGER.info(
                "Home Assistant will now open on the EverRise dashboard (/%s) for %s",
                panel_url_path,
                user.name,
            )
        except Exception:  # noqa: BLE001 - one bad user shouldn't stop the rest
            _LOGGER.exception("Could not set the default panel for user %s", user.name)
