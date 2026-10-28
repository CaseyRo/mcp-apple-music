"""The documented rail: api.music.apple.com with our own developer token.

Everything Apple documents goes through here: storefront, catalog and library
search, ISRC lookup, library playlist reads, playlist creation and appending
tracks. Resources come back as Apple's raw JSON objects (id, type, attributes).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from .devtoken import DeveloperToken
from .rails import API, RailClient, Read, Throttle, Write

ISRC_BATCH = 25  # Apple's cap on filter[isrc] values per request
SEARCH_LIMIT = 25  # Apple's cap on search results per type


class AppleMusicAPI(RailClient):
    rail = API
    base_url = "https://api.music.apple.com"

    def __init__(
        self,
        developer_token: DeveloperToken,
        user_token: Callable[[], str | None],
        on_user_token_rejected: Callable[[], None] = lambda: None,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        super().__init__(user_token, on_user_token_rejected, transport, throttle)
        self._dev = developer_token
        self._storefront: str | None = None

    async def _auth(self, user: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._dev.get()}"}
        if user:
            headers["Music-User-Token"] = self._music_user_token()
        return headers

    # -- reads ---------------------------------------------------------------

    async def storefront(self) -> str:
        if self._storefront is None:
            resp = await self._request("GET", "/v1/me/storefront", user=True)
            self._storefront = resp.json()["data"][0]["id"]
        return self._storefront

    async def _search(
        self, path: str, term: str, types, limit: int, user: bool
    ) -> Read:
        resp = await self._request(
            "GET",
            path,
            user=user,
            params={
                "term": term,
                "types": ",".join(types),
                "limit": min(limit, SEARCH_LIMIT),
            },
        )
        results = resp.json().get("results", {})
        return self.throttle.label(
            [r for t in types for r in results.get(t, {}).get("data", [])]
        )

    async def search_catalog(
        self, term: str, types: tuple[str, ...] = ("songs",), limit: int = SEARCH_LIMIT
    ) -> Read:
        sf = await self.storefront()
        return await self._search(f"/v1/catalog/{sf}/search", term, types, limit, False)

    async def search_library(
        self,
        term: str,
        types: tuple[str, ...] = ("library-songs",),
        limit: int = SEARCH_LIMIT,
    ) -> Read:
        return await self._search("/v1/me/library/search", term, types, limit, True)

    async def songs_by_isrc(self, isrcs: list[str]) -> Read:
        """Catalog songs matching the ISRCs. Apple silently omits the misses, and one
        ISRC may match several songs; callers group by ``attributes.isrc``."""
        sf = await self.storefront()
        songs: list[dict] = []
        for i in range(0, len(isrcs), ISRC_BATCH):
            resp = await self._request(
                "GET",
                f"/v1/catalog/{sf}/songs",
                params={"filter[isrc]": ",".join(isrcs[i : i + ISRC_BATCH])},
            )
            songs.extend(resp.json().get("data", []))
        return self.throttle.label(songs)

    async def library_playlists(self) -> Read:
        return self.throttle.label(
            await self._pages("/v1/me/library/playlists", limit=100)
        )

    async def library_playlist(self, playlist_id: str) -> dict:
        resp = await self._request(
            "GET", f"/v1/me/library/playlists/{playlist_id}", user=True
        )
        return resp.json()["data"][0]

    async def playlist_tracks(self, playlist_id: str) -> Read:
        """Tracks in playlist order. Apple answers 404 for an empty playlist."""
        return self.throttle.label(
            await self._pages(
                f"/v1/me/library/playlists/{playlist_id}/tracks", limit=100
            )
        )

    # -- writes --------------------------------------------------------------

    async def create_playlist(self, name: str, description: str = "") -> Write:
        attributes = {"name": name}
        if description:
            attributes["description"] = description
        resp = await self._request(
            "POST",
            "/v1/me/library/playlists",
            user=True,
            json={"attributes": attributes},
        )
        return Write(self.rail, resp.status_code, resp.json()["data"][0]["id"])

    async def append_tracks(self, playlist_id: str, song_ids: list[str]) -> Write:
        """Append catalog songs at the end of the playlist, in the given order."""
        if not song_ids:
            raise ValueError("no songs to append")
        resp = await self._request(
            "POST",
            f"/v1/me/library/playlists/{playlist_id}/tracks",
            user=True,
            json={"data": [{"id": s, "type": "songs"} for s in song_ids]},
        )
        return Write(self.rail, resp.status_code)
