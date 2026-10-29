"""Spotify Web API with client credentials: one track's ISRC and artists.

Batch lookups (`/v1/tracks?ids=`) answer 403 for this kind of app, so tracks are
fetched one by one; the match cache keeps that to about one call per new song.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

ACCOUNTS = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


class SpotifyError(Exception):
    pass


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._creds = (client_id, client_secret)
        self._http = httpx.AsyncClient(transport=transport, timeout=10)
        self._clock = clock
        self._token: tuple[str, float] | None = None  # value, expiry

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self) -> str:
        if self._token and self._token[1] > self._clock() + 60:
            return self._token[0]
        try:
            resp = await self._http.post(
                ACCOUNTS, data={"grant_type": "client_credentials"}, auth=self._creds
            )
        except httpx.HTTPError as e:
            raise SpotifyError(f"token request failed: {e}") from e
        if resp.status_code != 200:
            raise SpotifyError(f"token request: HTTP {resp.status_code}")
        body = resp.json()
        self._token = (body["access_token"], self._clock() + body["expires_in"])
        return self._token[0]

    async def track(self, spotify_id: str) -> dict:
        """``{title, artists: [names], album, duration_ms, isrc}``."""
        token = await self._bearer()
        try:
            resp = await self._http.get(
                f"{API}/tracks/{spotify_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            raise SpotifyError(f"track lookup failed: {e}") from e
        if resp.status_code == 401:
            self._token = None
        if resp.status_code != 200:
            raise SpotifyError(f"track {spotify_id}: HTTP {resp.status_code}")
        t = resp.json()
        return {
            "title": t.get("name", ""),
            "artists": [a["name"] for a in t.get("artists", []) if a.get("name")],
            "album": (t.get("album") or {}).get("name"),
            "duration_ms": t.get("duration_ms"),
            "isrc": (t.get("external_ids") or {}).get("isrc"),
        }
