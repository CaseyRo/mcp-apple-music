"""Finding the Apple song for a window entry (design D7): Spotify ID → ISRC →
Apple catalog, else a strict title-and-artist search. No other version is ever
substituted: no acceptable match means the track is unavailable."""

from __future__ import annotations

import re

from . import models as mo
from .apple_api import AppleMusicAPI
from .mirror import Match, _fold, event
from .rails import AppleError, RateLimited, UserTokenMissing, UserTokenRejected
from .spotify import SpotifyClient, SpotifyError
from .store import Store

MISS_TTL_SECONDS = 86_400
NO_MATCH = "no ISRC match and no acceptable title match"
ARTIST_MISMATCH = "title match rejected for artist mismatch"
_SUFFIX = re.compile(r"\s+-\s+.*$")  # "- Remastered 2011", "- Radio Edit"
_BRACKETS = re.compile(r"\s*[(\[][^)\]]*[)\]]")  # "(feat. X)", "[Live]"


def norm_title(title: str) -> str:
    bare = _BRACKETS.sub("", _SUFFIX.sub("", title))
    return " ".join(re.findall(r"\w+", _fold(bare)))


def artists_overlap(artists: list[str], song: mo.Song) -> bool:
    return any(mo.matches(a, song.artist, *song.featured) for a in artists if a)


class Matcher:
    def __init__(
        self,
        api: AppleMusicAPI,
        store: Store,
        spotify: SpotifyClient | None = None,
        miss_ttl: float = MISS_TTL_SECONDS,
    ) -> None:
        self.api = api
        self.store = store
        self.spotify = spotify
        self.miss_ttl = miss_ttl

    async def __call__(self, entry) -> Match:
        key = entry["key"] or ""
        # An ID-only push needs Spotify's details, which the cache doesn't keep.
        use_cache = key and entry["title"]
        cached = self.store.cached_match(key, self.miss_ttl) if use_cache else None
        if cached:
            return Match(
                cached["apple_song_id"],
                cached["method"],
                cached["duration_ms"],
                cached["isrc"],
                cached["reason"],
            )
        match = await self._find(entry)
        if key:
            self.store.put_match(
                key,
                apple_song_id=match.apple_song_id,
                method=match.method,
                isrc=match.isrc,
                duration_ms=match.duration_ms,
                reason=match.reason,
            )
        return match

    async def _find(self, entry) -> Match:
        title, album = entry["title"], entry["album"]
        # The feed joins artists with commas; split only when Spotify can't say.
        artists = [a.strip() for a in (entry["artists"] or "").split(",")]
        isrc, duration = None, entry["duration_ms"]
        if entry["spotify_id"] and self.spotify:
            try:
                sp = await self.spotify.track(entry["spotify_id"])
            except SpotifyError as e:
                event(
                    "spotify_lookup_failed",
                    entry["mirror"],
                    spotify_id=entry["spotify_id"],
                    error=str(e),
                )
            else:
                title, artists, album = sp["title"] or title, sp["artists"], sp["album"]
                isrc, duration = sp["isrc"], duration or sp["duration_ms"]
        details = {"title": title, "artists": ", ".join(artists), "album": album}
        if not title:
            return Match(None, reason="no title, and Spotify could not complete it")
        try:
            if isrc:
                read = await self.api.songs_by_isrc([isrc])
                if read.data:
                    s = mo.song(read.data[0])
                    ms = _duration(read.data[0]) or duration
                    return Match(s.id, "isrc", ms, isrc, **details)
                if read.possibly_throttled:  # an empty answer may be the throttle
                    raise RateLimited("api", 429, "empty ISRC answer while throttled")
            return await self._by_title(
                entry["mirror"], title, artists, isrc, duration, details
            )
        except (RateLimited, UserTokenMissing, UserTokenRejected):
            raise
        except AppleError as e:
            if e.status and 400 <= e.status < 500:  # this track's query, not Apple
                return Match(
                    None, isrc=isrc, reason=f"Apple refused the search: {e.reason}"
                )
            raise

    async def _by_title(self, mirror, title, artists, isrc, duration, details) -> Match:
        read = await self.api.search_catalog(
            f"{norm_title(title)} {artists[0] if artists else ''}".strip(), limit=10
        )
        if not read.data and read.possibly_throttled:
            raise RateLimited("api", 429, "empty search answer while throttled")
        wanted = norm_title(title)
        same_title = [r for r in read.data if norm_title(mo.song(r).title) == wanted]
        ok = [r for r in same_title if artists_overlap(artists, mo.song(r))]
        if not ok:
            reason = ARTIST_MISMATCH if same_title else NO_MATCH
            return Match(
                None, isrc=isrc, reason=reason, duration_ms=duration, **details
            )
        first = mo.song(ok[0])
        method = "title" if len(ok) == 1 else "title-ambiguous"
        if len(ok) > 1:
            event(
                "ambiguous_match",
                mirror,
                title=title,
                chosen=f"{first.title} – {first.artist} ({first.id})",
                alternatives=[
                    f"{s.title} – {s.artist} ({s.id})" for s in map(mo.song, ok[1:])
                ],
            )
        return Match(first.id, method, _duration(ok[0]) or duration, isrc, **details)


def _duration(resource: dict) -> int | None:
    return resource.get("attributes", {}).get("durationInMillis")
