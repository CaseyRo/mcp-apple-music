"""The web-player rail: amp-api.music.apple.com, the host music.apple.com uses.

Only what the documented API cannot do goes through here: removing playlist tracks,
renaming and deleting playlists, plus the track listing whose IDs removal needs.
Every amp-api call in this server lives in this module (see docs/decisions/0001).

amp-api accepts the AMPWebPlay token embedded in the web player's public JS bundle,
not our own developer token, and needs ``Origin: https://music.apple.com``. The
token is harvested without a browser and refreshed before it expires; when that
fails the rail reports itself unavailable and the documented rail is unaffected.

Endpoint knowledge (paths, headers, token extraction, ``canEdit`` classification)
is ported from epheterson/applemusic-mcp, MIT License, Copyright (c) 2024 Eric
Pheterson, and rewritten here on httpx.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx
import jwt

from .rails import WEB, RailClient, RailUnavailable, Read, Throttle, Write

ORIGIN = "https://music.apple.com"
BROWSE_URL = f"{ORIGIN}/us/browse"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
# The web player's main chunk, e.g. /assets/index~6da982354d.js
_BUNDLE_RE = re.compile(r"/assets/index~[A-Za-z0-9]+\.js")
# A complete JWT whose payload starts with the AMPWebPlay issuer claim.
_TOKEN_RE = re.compile(
    r"eyJ0eXAiOiJKV1Q[A-Za-z0-9_-]+\.eyJpc3MiOiJBTVBXZWJQbGF5[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)
REFRESH_BEFORE_SECONDS = 7 * 86_400


def extract_web_token(bundle_js: str) -> str:
    m = _TOKEN_RE.search(bundle_js)
    if not m:
        raise ValueError("AMPWebPlay token not found in the web-player bundle")
    return m.group(0)


def playlist_kind(playlist: dict) -> str:
    """Who can write a library playlist, from either rail's resource:

    - ``api``: created through the API (``canEdit``); the only kind we edit.
    - ``user``: made in Music.app (no catalog globalId, or a ``pl.u-…`` one).
    - ``apple``: an Apple-curated playlist added to the library; read-only.
    """
    attrs = playlist.get("attributes", {})
    if attrs.get("canEdit"):
        return "api"
    global_id = (attrs.get("playParams") or {}).get("globalId") or ""
    if global_id and not global_id.startswith("pl.u-"):
        return "apple"
    return "user"


class AppleWebPlayer(RailClient):
    rail = WEB
    base_url = "https://amp-api.music.apple.com"

    def __init__(
        self,
        user_token: Callable[[], str | None],
        on_user_token_rejected: Callable[[], None] = lambda: None,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            user_token, on_user_token_rejected, transport, throttle, {"Origin": ORIGIN}
        )
        self._clock = clock
        self._token: str | None = None
        self._token_exp = 0.0
        self.last_error: str | None = None  # the last failed harvest, for /health

    async def _harvest(self) -> str:
        page = await self._http.get(BROWSE_URL, headers=_UA, follow_redirects=True)
        page.raise_for_status()
        m = _BUNDLE_RE.search(page.text)
        if not m:
            raise ValueError("web-player bundle not found on music.apple.com")
        bundle = await self._http.get(
            ORIGIN + m.group(0), headers=_UA, follow_redirects=True
        )
        bundle.raise_for_status()
        return extract_web_token(bundle.text)

    async def web_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_exp - REFRESH_BEFORE_SECONDS:
            return self._token
        try:
            token = await self._harvest()
            claims = jwt.decode(token, options={"verify_signature": False})
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
            self.last_error = str(e)
            # A failed refresh keeps the current token while it is still valid.
            if self._token and now < self._token_exp:
                return self._token
            raise RailUnavailable(
                self.rail, None, f"web-player token unavailable: {e}"
            ) from e
        self._token = token
        self._token_exp = float(claims.get("exp", now + 86_400))
        self.last_error = None
        return token

    def token_state(self) -> dict:
        """For /health: never the token, only whether one is held and until when."""
        if self._token and self._clock() < self._token_exp:
            return {"state": "valid", "expires_at": self._token_exp}
        if self.last_error:
            return {"state": "invalid", "error": self.last_error}
        return {"state": "not fetched yet"}  # harvested on the first web-rail call

    async def _auth(self, user: bool) -> dict[str, str]:
        mut = self._music_user_token()  # every amp-api call is user-scoped
        return {
            "Authorization": f"Bearer {await self.web_token()}",
            "Music-User-Token": mut,
        }

    def _drop_credential(self) -> bool:
        # Apple may rotate the bundle token before its exp; re-harvest once.
        self._token = None
        return True

    # -- reads ---------------------------------------------------------------

    async def library_playlists(self) -> Read:
        """Library playlists with `trackCount`, which the documented API refuses."""
        return self.throttle.label(
            await self._pages(
                "/v1/me/library/playlists", limit=100, extend="trackCount"
            )
        )

    async def playlist_tracks(self, playlist_id: str) -> Read:
        """Tracks in playlist order; each ``id`` is what `remove_track` takes.
        Every copy of a song carries the same ID."""
        return self.throttle.label(
            await self._pages(
                f"/v1/me/library/playlists/{playlist_id}/tracks", limit=100
            )
        )

    # -- writes --------------------------------------------------------------

    async def remove_track(
        self, playlist_id: str, track_id: str, every_copy: bool = False
    ) -> Write:
        """Remove the topmost copy of a track, or every copy.

        All copies of a song share one ID, so ``mode`` picks which go: ``first`` is
        exactly the topmost one (verified live 2026-10-28, ``[A, B, A]`` -> ``[B, A]``).
        """
        resp = await self._request(
            "DELETE",
            f"/v1/me/library/playlists/{playlist_id}/tracks",
            user=True,
            params={
                "ids[library-songs]": track_id,
                "mode": "all" if every_copy else "first",
            },
        )
        return Write(self.rail, resp.status_code)

    async def rename_playlist(self, playlist_id: str, name: str) -> Write:
        return await self._patch(playlist_id, {"name": name})

    async def set_playlist_description(
        self, playlist_id: str, description: str
    ) -> Write:
        # Sent as a string; Apple reads it back as {"standard": ...} (checked live).
        return await self._patch(playlist_id, {"description": description})

    async def _patch(self, playlist_id: str, attributes: dict) -> Write:
        resp = await self._request(
            "PATCH",
            f"/v1/me/library/playlists/{playlist_id}",
            user=True,
            json={"attributes": attributes},
        )
        return Write(self.rail, resp.status_code)

    async def delete_playlist(self, playlist_id: str) -> Write:
        resp = await self._request(
            "DELETE", f"/v1/me/library/playlists/{playlist_id}", user=True
        )
        return Write(self.rail, resp.status_code)
