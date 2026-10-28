"""Playlist writes judged by their effect, not by Apple's HTTP status.

Each write is bracketed by two reads of the playlist and succeeds only if the
occurrence counts moved as asked: up after an add, down after a removal. Counts
rather than presence, because a playlist may already hold the song (repeats are
normal in a rolling window), and because they hold whether a removal takes out
one copy or all of them. Apple's reads can lag its writes, so a disagreeing
read-back is retried with growing delays before it counts as a failure.
"""

from __future__ import annotations

import asyncio
import functools
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .apple_api import AppleMusicAPI
from .apple_web import AppleWebPlayer
from .rails import AppleError, Read

NOT_ADDED = "not present after write"
NOT_REMOVED = "still present after removal"


@dataclass
class Verified:
    rail: str
    ok: bool
    reason: str | None = None
    failed: list[str] = field(default_factory=list)  # ids whose write didn't take
    tracks: list[dict] | None = None  # the playlist as read back; None if unreadable


def catalog_id(track: dict) -> str | None:
    pp = track.get("attributes", {}).get("playParams") or {}
    return pp.get("catalogId") or pp.get("id")


def track_id(track: dict) -> str | None:
    return track.get("id")


class Library:
    def __init__(
        self,
        api: AppleMusicAPI,
        web: AppleWebPlayer,
        delays: tuple[float, ...] = (1.0, 2.0, 4.0),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.web = web
        self._delays = delays
        self._sleep = sleep

    async def add_tracks(self, playlist_id: str, song_ids: list[str]) -> Verified:
        """Append catalog songs; verified against the documented rail's listing."""
        read = functools.partial(self.api.playlist_tracks, playlist_id)
        before = Counter(map(catalog_id, (await read()).data))
        await self.api.append_tracks(playlist_id, song_ids)
        return await self._settle(
            self.api.rail, read, catalog_id, before, Counter(song_ids), +1, NOT_ADDED
        )

    async def remove_tracks(self, playlist_id: str, track_ids: list[str]) -> Verified:
        """Remove tracks by the IDs the web-player listing gives them."""
        read = functools.partial(self.web.playlist_tracks, playlist_id)
        before = Counter(map(track_id, (await read()).data))
        error: AppleError | None = None
        removed_any = False
        for tid in dict.fromkeys(track_ids):
            try:
                await self.web.remove_track(playlist_id, tid)
            except AppleError as e:
                if not removed_any:
                    raise  # nothing was written; the caller gets Apple's answer as is
                error = e  # some removals went through: report what the playlist shows
                break
            removed_any = True
        result = await self._settle(
            self.web.rail, read, track_id, before, Counter(track_ids), -1, NOT_REMOVED
        )
        if error and result.ok is False:
            result.reason = f"{result.reason}; stopped at: {error}"
        return result

    async def _settle(
        self,
        rail: str,
        read: Callable[[], Awaitable[Read]],
        key: Callable[[dict], str | None],
        before: Counter,
        need: Counter,
        direction: int,
        reason: str,
    ) -> Verified:
        failed: list[str] = list(need)
        tracks: list[dict] | None = None
        for delay in (0.0, *self._delays):
            if delay:
                await self._sleep(delay)
            try:
                tracks = (await read()).data
            except AppleError as e:
                return Verified(
                    rail,
                    False,
                    f"accepted by Apple, but the read-back failed: {e}",
                    list(need),
                )
            after = Counter(map(key, tracks))
            failed = [
                i for i, n in need.items() if (after[i] - before[i]) * direction < n
            ]
            if not failed:
                return Verified(rail, True, tracks=tracks)
        return Verified(rail, False, reason, failed, tracks)
