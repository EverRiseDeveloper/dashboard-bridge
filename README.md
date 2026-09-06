# Everrise Dashboard Config Bridge

A small Home Assistant custom integration that exposes an **authenticated**
read/write API for the [Everrise Dashboard](https://github.com/EverRiseDeveloper/dashboard)'s
`config.json` — the file that defines every room, camera, entity, and
threshold the dashboard uses.

## Why this exists

The dashboard used to read `config.json` as a plain static file under
`config/www/<dashboard-folder>/config.json`, which Home Assistant serves at
`/local/...` with **no login check** — anyone with that URL could read it,
authenticated or not. This integration moves the actual data file out of
`www/` entirely (to `config/everrise_dashboard/<folder>/config.json`, which
HA never maps to an HTTP path) and serves it instead through
`/api/everrise_dashboard/config`, which requires the same login as the rest
of Home Assistant. Writes additionally require an admin account.

## Installation

### Via HACS (custom repository)

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Add `https://github.com/EverRiseDeveloper/dashboard-bridge`, category **Integration**.
3. Install "Everrise Dashboard Config Bridge", restart Home Assistant.

### Manual

Copy `custom_components/everrise_dashboard/` into your Home Assistant
`config/custom_components/` folder, then restart Home Assistant.

## Setup

Settings → Devices & Services → **Add Integration** → search "Everrise
Dashboard Config Bridge". Most installs can accept the defaults (folder
`everrise-dashboard`, filename `config.json`) — only change these if a
particular client's dashboard was deployed under a different folder name.

## Landing page

The integration registers the dashboard as a native Home Assistant panel at
`/everrise` — a `panel_custom` panel, **not** an iframe. Home Assistant hands
it the current user's already-authenticated `hass.connection` / `hass.auth`,
so there is no second browsing context, no separate login, and no token to
obtain.

On setup it also makes that panel the page Home Assistant **opens on**, so a
client lands in the EverRise dashboard the moment they open the Companion app
— iPhone, iPad, Android phone, Android tablet, and any browser. All of those
render the same Home Assistant frontend, so this is one instance-level
setting rather than per-device configuration.

It writes the same `default_panel` field Home Assistant's own UI writes
(Settings → Dashboards → "Set as default", and the per-user picker under
Profile → Dashboard). Those pickers only *list* Lovelace dashboards, but the
frontend resolves the stored value against every registered panel, so a
`panel_custom` panel is a valid value there.

- On Home Assistant versions with frontend **system** data, one write covers
  every user on the install, including users created later. A household
  member can still override it for themselves under Profile → Dashboard.
- On older versions it falls back to writing each existing user's own
  setting, skipping anyone who has already chosen a landing page. Users added
  after setup aren't covered on that path — reload the integration to pick
  them up.

Untick **"Open Home Assistant on the EverRise dashboard"** during setup, or
later under the integration's *Configure*, to leave the landing page alone.

## First-time migration

If you already have a `config.json` at
`config/www/<folder>/config.json` from before this integration existed,
move it to `config/everrise_dashboard/<folder>/config.json` (create that
folder) after installing — the integration reads/writes only the new
location, and does not migrate the old file automatically.

## API

- `GET /api/everrise_dashboard/config` — any logged-in user; returns the
  current config JSON, or `404` if nothing has been saved yet.
- `PUT` / `POST /api/everrise_dashboard/config` — admin users only; body is
  the full config JSON document, written atomically. Returns the saved
  document on success.

Deep validation of the dashboard's schema (rooms, cameras, header stats,
etc.) is intentionally **not** done here — the dashboard's own Admin UI
validates against its schema before ever sending a request. This API only
checks the body is a JSON object with `household` and `headerStats` keys,
then writes it.
