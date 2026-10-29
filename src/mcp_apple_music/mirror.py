"""The mirror engine: arrivals in, the playlist kept equal to the window (design D5, D8).

A window entry is one occurrence of a track. A sync matches new entries to Apple
songs, expires the oldest entries past the window length, then compares the
playlist with the window by song counts: copies beyond the window's count are
removed (Apple takes the topmost copy, which is the expired one), and entries
without a copy are appended. Anything other than an expiry or a new arrival is a
repair and is logged. Because every sync compares the whole playlist, the periodic
reconcile is the same sync run on a timer.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .library import NOT_ADDED, Library, catalog_id, track_id
from .mirror_config import Mirror
from .rails import (
    AppleError,
    RailUnavailable,
    RateLimited,
    UserTokenMissing,
    UserTokenRejected,
)
from .store import Store

DEDUPE_HORIZON_SECONDS = 3600
# shortcut: a pushed track with no duration and no Apple match counts as 4 minutes;
# upgrade if a source without durations ever carries many tracks Apple lacks.
DEFAULT_DURATION_MS = 240_000
MAX_REPAIRS = 10  # per sync; a bigger drift is likelier a bad read than real
APPEND_BATCH = 25
GROWTH_LIMIT = 1.5  # without removal, appends pause past this many windows
NOT_SHOWN = "Apple accepted the add, but the song never showed up in the playlist"
_ESCALATE = (RateLimited, UserTokenMissing, UserTokenRejected, RailUnavailable)


def event(kind: str, mirror: str, **fields) -> None:
    """One JSON line on stderr, next to the usage telemetry."""
    sys.stderr.write(json.dumps({"mirror_event": kind, "mirror": mirror, **fields}))
    sys.stderr.write("\n")
    sys.stderr.flush()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(
        "".join(c for c in decomposed if not unicodedata.combining(c)).split()
    )


@dataclass(frozen=True)
class Track:
    """A track as a source reports it."""

    title: str
    artists: str  # comma-joined, as the feed and webhook payloads carry it
    album: str | None = None
    spotify_id: str | None = None
    duration_ms: int | None = None

    @property
    def key(self) -> str:
        """What a double delivery is recognised by."""
        if self.spotify_id:
            return f"spotify:{self.spotify_id}"
        return f"text:{_fold(self.title)}|{_fold(self.artists)}"


@dataclass(frozen=True)
class Match:
    apple_song_id: str | None
    method: str | None = None  # isrc | title | title-ambiguous
    duration_ms: int | None = None
    isrc: str | None = None
    reason: str | None = None  # why there is no Apple song


Matcher = Callable[[sqlite3.Row], Awaitable[Match]]


@dataclass
class SyncResult:
    mirror: str
    playlist_id: str | None = None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def expire_count(durations: list[int | None], window_ms: int) -> int:
    """How many of the oldest entries leave so the rest fit within the window."""
    total = sum(d or DEFAULT_DURATION_MS for d in durations)
    n = 0
    while total > window_ms and n < len(durations):
        total -= durations[n] or DEFAULT_DURATION_MS
        n += 1
    return n


def label(e) -> str:
    return f"{e['title']} – {e['artists']}" if e["artists"] else e["title"]


def _playable(e) -> bool:
    return bool(e["apple_song_id"]) and not e["unavailable_reason"]


def _seed(track: dict) -> dict:
    a = track.get("attributes", {})
    return {
        "title": a.get("name", ""),
        "artists": a.get("artistName", ""),
        "album": a.get("albumName"),
        "duration_ms": a.get("durationInMillis"),
        "apple_song_id": catalog_id(track),
        "match_method": "adopted",
        "placed": 1,
    }


class Engine:
    def __init__(
        self,
        store: Store,
        library: Library,
        matcher: Matcher,
        horizon_seconds: float = DEDUPE_HORIZON_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.library = library
        self.matcher = matcher
        self.horizon = horizon_seconds
        self.clock = clock
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- arrivals ---------------------------------------------------------------

    def ingest(self, m: Mirror, tracks: list[Track], source: str) -> int:
        """Record arrivals, ignoring double deliveries; returns how many were new.
        Only SQLite: safe to call from a request handler that must answer fast."""
        new = 0
        for t in tracks:
            if self.store.arrived_within(m.name, t.key, self.horizon):
                continue
            self.store.add_arrival(m.name, t.key, t.duration_ms, source)
            self.store.add_entries(
                m.name,
                [
                    {
                        "key": t.key,
                        "spotify_id": t.spotify_id,
                        "title": t.title,
                        "artists": t.artists,
                        "album": t.album,
                        "duration_ms": t.duration_ms,
                    }
                ],
            )
            new += 1
        return new

    # -- the target playlist (design D12) ---------------------------------------

    async def setup(self, m: Mirror) -> str:
        """The mirror's playlist ID: adopted by ID, or created on the first run.
        Never found by name, so a same-named playlist is left alone."""
        row = self.store.mirror(m.name)
        if m.playlist_id:
            if not row or row["playlist_id"] != m.playlist_id:
                await self._adopt(m)
            return m.playlist_id
        if row and row["playlist_id"]:
            return row["playlist_id"]
        hours = m.window_seconds / 3600
        made = await self.library.api.create_playlist(
            m.playlist_name,
            f"The last {hours:g} hours of music, kept by mcp-apple-music. "
            "Start a few tracks in: the top track is the next to leave.",
        )
        self.store.own(made.id, f"created by mirror {m.name}")
        self.store.bind_mirror(m.name, made.id)
        event("created", m.name, playlist_id=made.id)
        return made.id

    async def _adopt(self, m: Mirror) -> None:
        api = self.library.api
        await api.library_playlist(m.playlist_id)  # raises if it isn't in the library
        tracks = (await api.playlist_tracks(m.playlist_id)).data
        self.store.own(m.playlist_id, f"adopted by mirror {m.name}")
        self.store.bind_mirror(m.name, m.playlist_id)
        # Its tracks are the oldest part of the window and leave by the window rule.
        self.store.add_entries(m.name, [_seed(t) for t in tracks], before=True)
        event("adopted", m.name, playlist_id=m.playlist_id, tracks=len(tracks))

    # -- sync -------------------------------------------------------------------

    async def sync(self, m: Mirror) -> SyncResult:
        async with self._locks[m.name]:
            res = SyncResult(m.name)
            try:
                res.playlist_id = await self.setup(m)
                await self._match(m, res)
                self._trim(m)
                await self._apply(m, res.playlist_id, res)
            except AppleError as e:
                self.store.record_sync(m.name, str(e))
                event("sync_failed", m.name, error=str(e))
                raise
            self.store.record_sync(m.name)
            return res

    async def _match(self, m: Mirror, res: SyncResult) -> None:
        for e in self.store.entries(m.name):
            if e["expired"] or e["apple_song_id"] or e["unavailable_reason"]:
                continue
            match = await self.matcher(e)
            fields = {"match_method": match.method, "isrc": match.isrc}
            if e["duration_ms"] is None:
                fields["duration_ms"] = match.duration_ms or DEFAULT_DURATION_MS
            if match.apple_song_id:
                fields["apple_song_id"] = match.apple_song_id
            else:
                fields["unavailable_reason"] = match.reason or "no Apple match"
                res.unavailable.append(label(e))
                event(
                    "skipped",
                    m.name,
                    title=e["title"],
                    artists=e["artists"],
                    album=e["album"],
                    spotify_id=e["spotify_id"],
                    isrc=match.isrc,
                    reason=fields["unavailable_reason"],
                )
            self.store.update_entry(m.name, e["seq"], **fields)

    def _trim(self, m: Mirror) -> None:
        live = [e for e in self.store.entries(m.name) if not e["expired"]]
        n = expire_count([e["duration_ms"] for e in live], m.window_seconds * 1000)
        for e in live[:n]:
            self.store.update_entry(m.name, e["seq"], expired=1)

    async def _apply(self, m: Mirror, pid: str, res: SyncResult) -> None:
        listing = (await self.library.api.playlist_tracks(pid)).data
        entries = self.store.entries(m.name)
        if not listing and any(e["placed"] for e in entries):
            # Apple lists a deleted playlist as empty: fail here instead of appending.
            await self.library.api.library_playlist(pid)
        actual = Counter(c for c in map(catalog_id, listing) if c)
        name = {
            catalog_id(t): t.get("attributes", {}).get("name", "?") for t in listing
        }
        lib_id = {catalog_id(t): track_id(t) for t in listing}
        live: defaultdict[str, list] = defaultdict(list)
        out: defaultdict[str, list] = defaultdict(list)  # expired, copy still listed
        for e in entries:
            if _playable(e) and not e["expired"]:
                live[e["apple_song_id"]].append(e)
            elif _playable(e) and e["placed"]:
                out[e["apple_song_id"]].append(e)

        # A song's copies are the expired entries' first (they are the topmost),
        # then the window's, oldest first; copies left over are strays.
        expected, strays, missing = Counter(), Counter(), []
        for song in dict.fromkeys([*actual, *live, *out]):  # playlist order first
            expected[song] = min(actual[song], len(out[song]))
            present = min(actual[song] - expected[song], len(live[song]))
            strays[song] = actual[song] - expected[song] - present
            for e in live[song][:present]:
                if not e["placed"]:
                    self.store.update_entry(m.name, e["seq"], placed=1)
            missing += live[song][present:]
        missing.sort(key=lambda e: e["seq"])
        drift = sum(strays.values()) + sum(1 for e in missing if e["placed"])
        if drift > MAX_REPAIRS:
            strays = Counter()
            missing = [e for e in missing if not e["placed"]]
            res.notes.append(
                f"The playlist differs from the window by {drift} tracks beyond expiry"
                " and new arrivals; left as is (more likely a bad read than real)."
            )
            event("drift_refused", m.name, drift=drift)

        removed, down = await self._remove(
            pid, +(expected + strays), lib_id, actual, res
        )
        for song, n in removed.items():
            res.removed += [name[song]] * n
            for _ in range(n - min(n, expected[song])):
                self._repair(m, res, "removed stray", f"{name[song]} ({song})")

        if down:
            size = sum(
                t.get("attributes", {}).get("durationInMillis", 0) for t in listing
            )
            if size > GROWTH_LIMIT * m.window_seconds * 1000:
                missing = []
                res.notes.append(
                    "Appends paused: removal isn't working and the playlist is past"
                    f" {GROWTH_LIMIT:g} times the window."
                )
        for i in range(0, len(missing), APPEND_BATCH):
            await self._append(m, pid, missing[i : i + APPEND_BATCH], res)

        # An expired entry is done once its copy is gone, or if it never had one.
        done = [
            e["seq"]
            for e in entries
            if e["expired"] and not (_playable(e) and e["placed"])
        ]
        for song, es in out.items():
            still_listed = expected[song] - min(removed[song], expected[song])
            done += [e["seq"] for e in es[: len(es) - still_listed]]
        self.store.delete_entries(m.name, done)

    async def _remove(
        self, pid: str, excess: Counter, lib_id: dict, actual: Counter, res: SyncResult
    ) -> tuple[Counter, bool]:
        """Remove the excess copies. Returns what went, and whether removal is down."""
        ids = [lib_id[s] for s, n in excess.items() for _ in range(n)]
        if not ids:
            return Counter(), False
        try:
            v = await self.library.remove_tracks(pid, ids)
        except RailUnavailable as e:
            res.notes.append(f"Removal unavailable: {e.reason}")
            return Counter(), True
        except (RateLimited, UserTokenMissing, UserTokenRejected):
            raise
        except AppleError as e:
            if not (e.status and 400 <= e.status < 500):
                raise
            res.notes.append(f"Removal refused: {e}")  # counts as down, not as fatal
            return Counter(), True
        if v.ok:
            return Counter(excess), False
        if v.stopped:
            res.notes.append(f"Removal stopped part-way: {v.stopped}")
        if v.tracks is None:  # unreadable: the next sync recounts
            res.failed += [f"remove {s}" for s in excess]
            return Counter(), False
        left = Counter(map(track_id, v.tracks))
        gone = Counter({s: max(0, actual[s] - left[lib_id[s]]) for s in excess})
        res.failed += [f"remove {s}" for s in excess if gone[s] < excess[s]]
        return +gone, not gone

    async def _append(self, m: Mirror, pid: str, batch: list, res: SyncResult) -> None:
        try:
            v = await self.library.add_tracks(pid, [e["apple_song_id"] for e in batch])
        except _ESCALATE:
            raise
        except AppleError as err:
            if not (err.status and 400 <= err.status < 500):
                raise
            if len(batch) > 1:  # find the one Apple refuses; the rest go in
                for e in batch:
                    await self._append(m, pid, [e], res)
                return
            self._unavailable(m, batch[0], f"Apple refused the add: {err.reason}", res)
            return
        failed = Counter(v.failed)
        for e in batch:
            song = e["apple_song_id"]
            if failed[song] and v.reason == NOT_ADDED:
                failed[song] -= 1
                self._unavailable(m, e, NOT_SHOWN, res)
            elif failed[song]:  # accepted, but the read-back failed: recount next sync
                failed[song] -= 1
                res.failed.append(label(e))
            else:
                self.store.update_entry(m.name, e["seq"], placed=1)
                res.added.append(label(e))
                if e["placed"]:  # it had been in the playlist before
                    self._repair(m, res, "re-appended", label(e))

    def _unavailable(self, m: Mirror, e, reason: str, res: SyncResult) -> None:
        self.store.update_entry(m.name, e["seq"], unavailable_reason=reason)
        res.unavailable.append(label(e))
        event("skipped", m.name, title=e["title"], artists=e["artists"], reason=reason)

    def _repair(self, m: Mirror, res: SyncResult, action: str, detail: str) -> None:
        self.store.add_repair(m.name, action, detail)
        res.repairs.append(f"{action}: {detail}")
        event("repair", m.name, action=action, detail=detail)
