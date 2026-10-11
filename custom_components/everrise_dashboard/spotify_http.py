"""Authenticated HTTP API for Spotify, for the Media page's Spotify browser.

Home Assistant's Spotify integration only lets anything look inside the
household's library while Spotify is already playing somewhere, and it has
no search. So the bridge talks to Spotify's Web API itself, with the Spotify
integration's own login: the household's OAuth token, which Home
Assistant's OAuth helpers keep fresh. That way the dashboard can list
playlists and their songs, Liked songs, Recently played and Top songs,
search, and start exactly what was picked on any of Spotify's speakers,
even when nothing is playing.

Any signed-in household member may use it (the same as headlines_http.py).
The token never leaves Home Assistant: the dashboard only ever gets names,
pictures and spotify: URIs back. Nothing is stored, and nothing about the
home is sent to Spotify.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

API_BASE = "/api/everrise_dashboard"
SPOTIFY_API = "https://api.spotify.com/v1"
TIMEOUT = ClientTimeout(total=12)

PAGE = 50
SEARCH_LIMIT = 8
MAX_QUEUE = 50

ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
URI_RE = re.compile(r"^spotify:(track|episode|album|playlist|artist|show):[A-Za-z0-9]{1,64}$")
CONTEXT_RE = re.compile(r"^spotify:((album|playlist|artist|show):[A-Za-z0-9]{1,64}|user:[A-Za-z0-9._-]{1,64}:collection)$")

LIBRARY_KINDS = ("playlists", "liked", "recent", "top")


class SpotifyError(Exception):
    """Something the dashboard can say in plain words, by its code."""

    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


# ---- Talking to Spotify ---------------------------------------------------


async def _token(hass: HomeAssistant) -> str:
    """The household's Spotify access token, refreshed if it's due."""
    entries = [e for e in hass.config_entries.async_entries("spotify") if e.state is ConfigEntryState.LOADED]
    if not entries:
        raise SpotifyError("not_set_up", 404)
    entry = entries[0]
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
        session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        await session.async_ensure_token_valid()
    except Exception as err:  # noqa: BLE001 - any failure here means "signed out"
        _LOGGER.debug("Spotify token unavailable: %r", err)
        raise SpotifyError("signed_out", 401) from err
    return str(session.token["access_token"])


async def _api(
    hass: HomeAssistant,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    token = await _token(hass)
    try:
        async with async_get_clientsession(hass).request(
            method,
            f"{SPOTIFY_API}{path}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status == 204:
                return None
            if resp.status >= 400:
                reason = ""
                try:
                    reason = str(((await resp.json()).get("error") or {}).get("reason") or "")
                except (ClientError, ValueError, AttributeError):
                    pass
                _LOGGER.debug("Spotify %s %s: %s %s", method, path, resp.status, reason)
                if resp.status == 401:
                    raise SpotifyError("signed_out", 401)
                if resp.status == 403:
                    raise SpotifyError("premium_required" if reason == "PREMIUM_REQUIRED" else "not_allowed", 403)
                if resp.status == 404:
                    raise SpotifyError("no_device" if reason == "NO_ACTIVE_DEVICE" else "not_found", 404)
                if resp.status == 429:
                    raise SpotifyError("busy", 429)
                raise SpotifyError("failed", 502)
            if resp.content_type != "application/json":
                return None
            return await resp.json()
    except ClientError as err:
        _LOGGER.debug("Spotify %s %s failed: %r", method, path, err)
        raise SpotifyError("unreachable", 502) from err
    except TimeoutError as err:
        raise SpotifyError("unreachable", 504) from err


# ---- What the dashboard gets ------------------------------------------------


def _image(images: Any) -> str | None:
    """The smallest picture that's still sharp on a phone (Spotify lists
    them largest first)."""
    if not isinstance(images, list) or not images:
        return None
    usable = [i for i in images if isinstance(i, dict) and i.get("url")]
    if not usable:
        return None
    sized = [i for i in usable if (i.get("width") or 0) >= 160]
    return str((sized[-1] if sized else usable[0])["url"])


def _artists(item: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name"))


def _track(t: Any, image: str | None = None) -> dict[str, Any] | None:
    if not isinstance(t, dict) or t.get("is_local") or not isinstance(t.get("uri"), str):
        return None
    if not URI_RE.match(t["uri"]):
        return None
    return {
        "kind": "track",
        "uri": t["uri"],
        "name": t.get("name") or "",
        "subtitle": _artists(t),
        "image": _image((t.get("album") or {}).get("images")) or image,
        "duration": int((t.get("duration_ms") or 0) / 1000),
    }


def _playlist(p: Any) -> dict[str, Any] | None:
    if not isinstance(p, dict) or not p.get("id") or not p.get("uri"):
        return None
    total = (p.get("tracks") or p.get("items") or {}).get("total")
    owner = (p.get("owner") or {}).get("display_name")
    subtitle = f"{total} songs" if isinstance(total, int) else (owner or "Playlist")
    return {"kind": "playlist", "id": p["id"], "uri": p["uri"], "name": p.get("name") or "", "subtitle": subtitle, "image": _image(p.get("images"))}


def _album(a: Any) -> dict[str, Any] | None:
    if not isinstance(a, dict) or not a.get("id") or not a.get("uri"):
        return None
    return {"kind": "album", "id": a["id"], "uri": a["uri"], "name": a.get("name") or "", "subtitle": _artists(a), "image": _image(a.get("images"))}


def _artist(a: Any) -> dict[str, Any] | None:
    if not isinstance(a, dict) or not a.get("uri"):
        return None
    return {"kind": "artist", "id": a.get("id"), "uri": a["uri"], "name": a.get("name") or "", "subtitle": "Artist", "image": _image(a.get("images"))}


def _kept(items: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if item and item["uri"] not in seen:
            seen.add(item["uri"])
            out.append(item)
    return out


def _offset(request: web.Request) -> int:
    try:
        return max(0, min(int(request.query.get("offset", "0")), 10_000))
    except ValueError:
        return 0


def _error(err: SpotifyError) -> web.Response:
    return web.json_response({"error": err.code}, status=err.status)


# ---- Views --------------------------------------------------------------------


class EverriseSpotifyDevicesView(HomeAssistantView):
    """GET {devices: [{id, name, type, active, restricted}]}: the speakers
    Spotify can play on right now."""

    url = f"{API_BASE}/spotify/devices"
    name = "api:everrise_dashboard:spotify:devices"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        try:
            data = await _api(self._hass, "GET", "/me/player/devices") or {}
        except SpotifyError as err:
            return _error(err)
        devices = [
            {
                "id": d.get("id"),
                "name": d.get("name") or "",
                "type": d.get("type") or "",
                "active": bool(d.get("is_active")),
                "restricted": bool(d.get("is_restricted")),
            }
            for d in data.get("devices") or []
            if isinstance(d, dict) and isinstance(d.get("id"), str)
        ]
        return self.json({"devices": devices})


class EverriseSpotifyLibraryView(HomeAssistantView):
    """GET ?kind=playlists|liked|recent|top&offset=N: {items, more}."""

    url = f"{API_BASE}/spotify/library"
    name = "api:everrise_dashboard:spotify:library"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        kind = request.query.get("kind", "playlists")
        if kind not in LIBRARY_KINDS:
            return self.json({"error": "bad_request"}, status_code=400)
        offset = _offset(request)
        try:
            if kind == "playlists":
                data = await _api(self._hass, "GET", "/me/playlists", params={"limit": PAGE, "offset": offset}) or {}
                items = _kept([_playlist(p) for p in data.get("items") or []])
                more = bool(data.get("next"))
            elif kind == "liked":
                data = await _api(self._hass, "GET", "/me/tracks", params={"limit": PAGE, "offset": offset, "market": "from_token"}) or {}
                items = _kept([_track((i or {}).get("track")) for i in data.get("items") or []])
                more = bool(data.get("next"))
            elif kind == "recent":
                data = await _api(self._hass, "GET", "/me/player/recently-played", params={"limit": PAGE}) or {}
                items = _kept([_track((i or {}).get("track")) for i in data.get("items") or []])
                more = False
            else:
                data = await _api(self._hass, "GET", "/me/top/tracks", params={"limit": PAGE, "offset": offset, "time_range": "short_term"}) or {}
                items = _kept([_track(t) for t in data.get("items") or []])
                more = bool(data.get("next"))
        except SpotifyError as err:
            return _error(err)
        return self.json({"items": items, "more": more})


class EverriseSpotifyPlaylistView(HomeAssistantView):
    """GET /spotify/playlist/<id>?offset=N: {name, uri, image, items, more}.
    Spotify won't list the songs in some of its own playlists for apps like
    this one; then items is empty and listed is false, and the playlist can
    still be played as a whole."""

    url = API_BASE + "/spotify/playlist/{playlist_id}"
    name = "api:everrise_dashboard:spotify:playlist"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, playlist_id: str) -> web.Response:
        if not ID_RE.match(playlist_id):
            return self.json({"error": "bad_request"}, status_code=400)
        offset = _offset(request)
        try:
            head = await _api(self._hass, "GET", f"/playlists/{playlist_id}", params={"fields": "name,uri,images"}) or {}
        except SpotifyError as err:
            return _error(err)
        result: dict[str, Any] = {"name": head.get("name") or "", "uri": head.get("uri") or f"spotify:playlist:{playlist_id}", "image": _image(head.get("images"))}
        try:
            data = await _api(
                self._hass,
                "GET",
                f"/playlists/{playlist_id}/tracks",
                params={"limit": PAGE * 2, "offset": offset, "market": "from_token"},
            ) or {}
        except SpotifyError as err:
            if err.code not in ("not_found", "not_allowed"):
                return _error(err)
            return self.json({**result, "items": [], "more": False, "listed": False})
        items = _kept([_track((i or {}).get("track") or (i or {}).get("item")) for i in data.get("items") or []])
        return self.json({**result, "items": items, "more": bool(data.get("next")), "listed": True})


class EverriseSpotifyAlbumView(HomeAssistantView):
    """GET /spotify/album/<id>: {name, uri, image, items}."""

    url = API_BASE + "/spotify/album/{album_id}"
    name = "api:everrise_dashboard:spotify:album"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, album_id: str) -> web.Response:
        if not ID_RE.match(album_id):
            return self.json({"error": "bad_request"}, status_code=400)
        try:
            data = await _api(self._hass, "GET", f"/albums/{album_id}", params={"market": "from_token"}) or {}
        except SpotifyError as err:
            return _error(err)
        image = _image(data.get("images"))
        tracks = (data.get("tracks") or {}).get("items") or []
        return self.json(
            {
                "name": data.get("name") or "",
                "uri": data.get("uri") or f"spotify:album:{album_id}",
                "image": image,
                "items": _kept([_track(t, image) for t in tracks]),
                "more": False,
                "listed": True,
            }
        )


class EverriseSpotifySearchView(HomeAssistantView):
    """GET ?q=...: {tracks, playlists, albums, artists}, a few of each."""

    url = f"{API_BASE}/spotify/search"
    name = "api:everrise_dashboard:spotify:search"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        q = " ".join(request.query.get("q", "").split())[:100]
        if not q:
            return self.json({"tracks": [], "playlists": [], "albums": [], "artists": []})
        try:
            data = await _api(
                self._hass,
                "GET",
                "/search",
                params={"q": q, "type": "track,playlist,album,artist", "limit": SEARCH_LIMIT, "market": "from_token"},
            ) or {}
        except SpotifyError as err:
            return _error(err)
        return self.json(
            {
                "tracks": _kept([_track(t) for t in (data.get("tracks") or {}).get("items") or []]),
                "playlists": _kept([_playlist(p) for p in (data.get("playlists") or {}).get("items") or []]),
                "albums": _kept([_album(a) for a in (data.get("albums") or {}).get("items") or []]),
                "artists": _kept([_artist(a) for a in (data.get("artists") or {}).get("items") or []]),
            }
        )


class EverriseSpotifyPlayView(HomeAssistantView):
    """POST {device_id, context_uri?, offset_uri?, uris?}: start it on that
    speaker. A playlist, album or artist plays as a whole, from offset_uri
    when a song in it was picked; a list of songs plays in that order. If
    Spotify has gone quiet on the speaker, it's woken first and asked
    again."""

    url = f"{API_BASE}/spotify/play"
    name = "api:everrise_dashboard:spotify:play"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "bad_request"}, status_code=400)
        if not isinstance(data, dict):
            return self.json({"error": "bad_request"}, status_code=400)
        device = data.get("device_id")
        context = data.get("context_uri")
        offset = data.get("offset_uri")
        uris = data.get("uris")
        if not isinstance(device, str) or not DEVICE_RE.match(device):
            return self.json({"error": "bad_request"}, status_code=400)
        body: dict[str, Any]
        if context is not None:
            if not isinstance(context, str) or not CONTEXT_RE.match(context):
                return self.json({"error": "bad_request"}, status_code=400)
            body = {"context_uri": context}
            if offset is not None:
                if not isinstance(offset, str) or not URI_RE.match(offset):
                    return self.json({"error": "bad_request"}, status_code=400)
                body["offset"] = {"uri": offset}
        elif isinstance(uris, list) and uris and len(uris) <= MAX_QUEUE and all(isinstance(u, str) and URI_RE.match(u) for u in uris):
            body = {"uris": uris}
        else:
            return self.json({"error": "bad_request"}, status_code=400)

        try:
            await _api(self._hass, "PUT", "/me/player/play", params={"device_id": device}, body=body)
        except SpotifyError as err:
            if err.code not in ("no_device", "not_found"):
                return _error(err)
            # The speaker has dropped out of Spotify's session: wake it there
            # first, then ask again once.
            try:
                await _api(self._hass, "PUT", "/me/player", body={"device_ids": [device], "play": False})
                await _api(self._hass, "PUT", "/me/player/play", params={"device_id": device}, body=body)
            except SpotifyError as again:
                return _error(again)
        return self.json({"ok": True})
