"""The mirror engine against an in-memory Apple with Apple's playlist semantics."""

from __future__ import annotations

from collections import Counter

import pytest

from mcp_apple_music.library import NOT_ADDED, Verified
from mcp_apple_music.mirror import (
    MAX_REPAIRS,
    NOT_SHOWN,
    Engine,
    Match,
    Track,
    expire_count,
)
from mcp_apple_music.mirror_config import Mirror
from mcp_apple_music.rails import AppleError, RailUnavailable, Read, Write
from mcp_apple_music.store import Store

from .fakes import Clock

MIN = 60_000  # ms


def song(catalog: str, minutes: float = 1) -> dict:
    return {
        "id": f"i.{catalog}",
        "type": "library-songs",
        "attributes": {
            "name": catalog,
            "artistName": "Artist",
            "durationInMillis": int(minutes * MIN),
            "playParams": {
                "id": f"i.{catalog}",
                "isLibrary": True,
                "catalogId": catalog,
            },
        },
    }


class FakeApple:
    """Append at the end; a removal takes the topmost copy; copies share an ID.
    Serves as both the documented API (``.api``) and the verified Library."""

    def __init__(self) -> None:
        self.api = self
        self.playlists: dict[str, list[str]] = {}
        self.names: dict[str, str] = {}
        self.never_listed: set[str] = set()  # accepted, but never shows up
        self.unremovable: set[str] = set()  # removal answered with a 404
        self.refused: set[str] = set()  # answered with a 400
        self.listed_as: dict[str, str] = {}  # the library's copy goes in instead
        self.web_down = False
        self.writes: list[tuple] = []

    async def create_playlist(self, name: str, description: str = "") -> Write:
        pid = f"p.{len(self.playlists) + 1}"
        self.playlists[pid], self.names[pid] = [], name
        self.writes.append(("create", name))
        return Write("api", 201, pid)

    async def library_playlist(self, pid: str) -> dict:
        if pid not in self.playlists:
            raise AppleError("api", 404, "Resource Not Found")
        return {"id": pid, "attributes": {"name": self.names[pid]}}

    async def playlist_tracks(
        self, pid: str
    ) -> Read:  # Apple lists a deleted one as empty
        return Read([song(c) for c in self.playlists.get(pid, [])])

    async def add_tracks(self, pid: str, ids: list[str]) -> Verified:
        if self.refused & set(ids):
            raise AppleError("api", 400, "Invalid song id")
        self.writes.append(("add", *ids))
        shown = [self.listed_as.get(c, c) for c in ids if c not in self.never_listed]
        self.playlists[pid] += shown
        failed = [c for c in ids if c in self.never_listed]
        if failed:
            return Verified("api", False, NOT_ADDED, failed)
        return Verified("api", True, listed=shown)

    async def remove_tracks(self, pid: str, lib_ids: list[str]) -> Verified:
        if self.web_down:
            raise RailUnavailable("web", None, "Web player token unavailable")
        if self.unremovable & set(lib_ids):
            raise AppleError("web", 404, "Resource Not Found")
        self.writes.append(("remove", *lib_ids))
        for lid in lib_ids:
            self.playlists[pid].remove(lid.removeprefix("i."))  # the topmost copy
        return Verified("web", True)


class Matcher:
    """Catalog ID = title; titles in ``missing`` have no Apple song."""

    def __init__(self) -> None:
        self.missing: set[str] = set()
        self.calls = 0

    async def __call__(self, entry) -> Match:
        self.calls += 1
        if entry["title"] in self.missing:
            return Match(None, reason="no ISRC match and no acceptable title match")
        return Match(entry["title"], "isrc", duration_ms=MIN, isrc="NLAA12500001")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def apple() -> FakeApple:
    return FakeApple()


@pytest.fixture
def matcher() -> Matcher:
    return Matcher()


@pytest.fixture
def engine(tmp_path, clock, apple, matcher):
    store = Store(tmp_path / "state.db", clock=clock)
    yield Engine(store, apple, matcher, clock=clock)
    store.close()


def rock(**kw) -> Mirror:
    return Mirror.model_validate(
        {
            "name": "rock",
            "playlist_name": "Station Rock",
            "window_seconds": 300,  # five one-minute tracks
            "source": {"type": "webhook"},
            **kw,
        }
    )


def push(engine, m, *titles, minutes: float = 1) -> int:
    return engine.ingest(
        m,
        [
            Track(t, "Artist", spotify_id=f"sp{t}", duration_ms=int(minutes * MIN))
            for t in titles
        ],
        "webhook",
    )


def test_expire_count_trims_to_86400_seconds():
    hour = 3_600_000
    assert expire_count([hour] * 24, 86_400_000) == 0
    assert expire_count([hour] * 25, 86_400_000) == 1
    assert expire_count([2 * hour, hour, None], 3 * hour) == 1  # None counts 4 minutes
    assert expire_count([], 86_400_000) == 0


def test_double_delivery_within_the_hour_is_ignored(engine, clock):
    m = rock()
    assert push(engine, m, "A") == 1
    clock.now += 120  # the feed reports what the webhook already pushed
    assert engine.ingest(m, [Track("A", "Artist", spotify_id="spA")], "feed") == 0
    assert len(engine.store.entries("rock")) == 1


def test_heavy_rotation_repeat_after_8_hours_is_kept(engine, clock, apple):
    m = rock(window_seconds=86_400)
    push(engine, m, "A", "B")
    clock.now += 8 * 3600
    assert push(engine, m, "A") == 1


async def test_first_run_creates_its_own_playlist(engine, apple):
    apple.playlists["p.foreign"] = ["X"]  # same name, not ours
    apple.names["p.foreign"] = "Station Rock"
    m = rock()
    push(engine, m, "A", "B")
    res = await engine.sync(m)
    assert res.playlist_id != "p.foreign" and res.added == ["A – Artist", "B – Artist"]
    assert apple.playlists[res.playlist_id] == ["A", "B"]
    assert apple.playlists["p.foreign"] == ["X"]
    assert engine.store.owns(res.playlist_id) and not engine.store.owns("p.foreign")
    assert (await engine.sync(m)).playlist_id == res.playlist_id  # kept by ID
    assert [w[0] for w in apple.writes].count("create") == 1


async def test_adoption_keeps_the_playlist_and_writes_nothing(engine, apple):
    apple.playlists["p.rock"] = ["O1", "O2", "O3"]
    apple.names["p.rock"] = "Station ▷ Rock"
    m = rock(playlist_id="p.rock")
    res = await engine.sync(m)
    assert res.playlist_id == "p.rock" and apple.writes == []
    assert engine.store.owns("p.rock")
    assert [e["match_method"] for e in engine.store.entries("rock")] == ["adopted"] * 3


async def test_adopted_tracks_leave_by_the_window_rule(engine, apple):
    """Five minutes of old tracks; each arriving minute pushes out one old minute."""
    apple.playlists["p.rock"] = ["O1", "O2", "O3", "O4", "O5"]
    apple.names["p.rock"] = "Station ▷ Rock"
    m = rock(playlist_id="p.rock")
    await engine.sync(m)
    push(engine, m, "N1")
    res = await engine.sync(m)
    assert apple.playlists["p.rock"] == ["O2", "O3", "O4", "O5", "N1"]
    assert (res.removed, res.added, res.repairs) == (["O1"], ["N1 – Artist"], [])


async def test_adoption_of_a_missing_playlist_fails(engine):
    with pytest.raises(AppleError, match="Not Found"):
        await engine.sync(rock(playlist_id="p.gone"))
    assert not engine.store.owns("p.gone")
    assert "Not Found" in engine.store.mirror("rock")["last_error"]


async def test_push_before_adoption_sorts_after_the_adopted_tracks(engine, apple):
    apple.playlists["p.rock"] = ["O1"]
    apple.names["p.rock"] = "x"
    m = rock(playlist_id="p.rock")
    push(engine, m, "N1")  # the webhook was faster than the first sync
    await engine.sync(m)
    assert apple.playlists["p.rock"] == ["O1", "N1"]


async def test_append_and_expire(engine, apple):
    m = rock()
    push(engine, m, "A", "B", "C", "D", "E")
    pid = (await engine.sync(m)).playlist_id
    push(engine, m, "F", minutes=2)  # one track enters, two leave
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["C", "D", "E", "F"]
    assert (res.removed, res.added) == (["A", "B"], ["F – Artist"])


async def test_only_the_expired_copy_of_a_repeat_is_removed(engine, apple, clock):
    m = rock()
    push(engine, m, "A", "B", "C")
    clock.now += 2 * 3600
    push(engine, m, "A", "D")
    pid = (await engine.sync(m)).playlist_id
    push(engine, m, "E")
    await engine.sync(m)
    assert apple.playlists[pid] == ["B", "C", "A", "D", "E"]


async def test_repeat_arriving_as_its_old_copy_expires_moves_to_the_end(
    engine, apple, clock
):
    m = rock()
    push(engine, m, "A", "B", "C", "D", "E")
    pid = (await engine.sync(m)).playlist_id
    clock.now += 2 * 3600
    push(engine, m, "A")
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["B", "C", "D", "E", "A"]
    assert (res.removed, res.added, res.repairs) == (["A"], ["A – Artist"], [])


async def test_reconcile_repairs_drift(engine, apple):
    m = rock()
    push(engine, m, "A", "B", "C")
    pid = (await engine.sync(m)).playlist_id
    apple.playlists[pid] = [
        "A",
        "X",
        "C",
    ]  # B lost after a failed write, X added by hand
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["A", "C", "B"]
    assert sorted(res.repairs) == ["re-appended: B – Artist", "removed stray: X (X)"]
    assert [r["action"] for r in engine.store.repairs("rock")] == [
        "re-appended",
        "removed stray",
    ]


async def test_large_drift_is_left_alone(engine, apple):
    m = rock(window_seconds=86_400)
    push(engine, m, "A")
    pid = (await engine.sync(m)).playlist_id
    strays = [f"X{i}" for i in range(MAX_REPAIRS + 1)]
    apple.playlists[pid] += strays
    push(engine, m, "B")
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["A", *strays, "B"]  # new arrivals still go in
    assert res.removed == [] and "left as is" in res.notes[0]


async def test_unavailable_track_is_skipped_but_counts_for_the_window(
    engine, apple, matcher
):
    matcher.missing = {"B"}
    m = rock()
    push(engine, m, "A", "B", "C", "D", "E")
    pid = (await engine.sync(m)).playlist_id
    assert apple.playlists[pid] == ["A", "C", "D", "E"]
    push(engine, m, "F")  # B's minute still counts, so A leaves
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["C", "D", "E", "F"]
    assert res.removed == ["A"]
    reasons = [e["unavailable_reason"] for e in engine.store.entries("rock")]
    assert reasons.count("no ISRC match and no acceptable title match") == 1
    assert matcher.calls == 6  # each entry matched once


async def test_add_that_never_shows_up_stops_retrying(engine, apple):
    apple.never_listed = {"B"}
    m = rock()
    push(engine, m, "A", "B")
    res = await engine.sync(m)
    assert res.added == ["A – Artist"] and res.unavailable == ["B – Artist"]
    await engine.sync(m)
    assert Counter(w[0] for w in apple.writes)["add"] == 1  # no churn


async def test_one_refused_song_does_not_block_the_batch(engine, apple):
    apple.refused = {"B"}
    m = rock()
    push(engine, m, "A", "B", "C")
    res = await engine.sync(m)
    assert apple.playlists[res.playlist_id] == ["A", "C"]
    (entry,) = [e for e in engine.store.entries("rock") if e["title"] == "B"]
    assert entry["unavailable_reason"] == "Apple refused the add: Invalid song id"


async def test_without_removal_appends_stop_at_one_and_a_half_windows(engine, apple):
    m = rock()
    push(engine, m, "A", "B", "C", "D", "E")
    pid = (await engine.sync(m)).playlist_id
    apple.web_down = True
    for t in ["F", "G", "H"]:
        push(engine, m, t)
        res = await engine.sync(m)
    assert apple.playlists[pid] == ["A", "B", "C", "D", "E", "F", "G", "H"]
    push(engine, m, "I")
    res = await engine.sync(m)
    assert "I" not in apple.playlists[pid] and "Appends paused" in res.notes[-1]
    apple.web_down = False  # back: the expired copies go, the backlog goes in
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["E", "F", "G", "H", "I"]


def test_not_shown_reason_is_specific():
    assert "never showed up" in NOT_SHOWN


async def test_refused_removal_does_not_stop_the_mirror(engine, apple):
    m = rock()
    push(engine, m, "A", "B", "C", "D", "E")
    pid = (await engine.sync(m)).playlist_id
    apple.unremovable = {"i.A"}
    push(engine, m, "F")
    res = await engine.sync(m)
    assert apple.playlists[pid] == ["A", "B", "C", "D", "E", "F"]
    assert "Removal refused" in res.notes[0]


async def test_deleted_playlist_fails_the_sync_instead_of_appending(engine, apple):
    m = rock()
    push(engine, m, "A")
    pid = (await engine.sync(m)).playlist_id
    del apple.playlists[pid]
    push(engine, m, "B")
    with pytest.raises(AppleError, match="Not Found"):
        await engine.sync(m)
    assert all(e["unavailable_reason"] is None for e in engine.store.entries("rock"))


async def test_song_listed_under_another_id_is_followed_not_churned(engine, apple):
    apple.listed_as = {"B": "B-library"}
    m = rock()
    push(engine, m, "A", "B")
    pid = (await engine.sync(m)).playlist_id
    assert apple.playlists[pid] == ["A", "B-library"]
    res = await engine.sync(m)  # the next sync sees the copy as B, not as a stray
    assert (res.removed, res.added, res.repairs) == ([], [], [])
    (b,) = [e for e in engine.store.entries("rock") if e["title"] == "B"]
    assert (b["apple_song_id"], b["placed"], b["unavailable_reason"]) == (
        "B-library",
        1,
        None,
    )


# -- short and long songs (Blur's "Song 2" is 2:01, Supertramp's "Fool's Overture" 10:55)


async def test_long_arrival_pushes_out_several_short_tracks_at_once(engine, apple):
    """The top moves by the arriving track's length, not by one track: a lead of
    "2-3 tracks" is gone when those are short and a long one arrives."""
    m = rock(window_seconds=15 * 60)
    for t in ["S1", "S2", "S3", "S4", "S5"]:
        push(engine, m, t, minutes=2)
    pid = (await engine.sync(m)).playlist_id
    push(engine, m, "Fools-Overture", minutes=11)
    res = await engine.sync(m)
    assert res.removed == ["S1", "S2", "S3"]  # 6 of 10 minutes leave in one sync
    assert apple.playlists[pid] == ["S4", "S5", "Fools-Overture"]


async def test_short_arrival_pushes_out_a_long_top_track(engine, apple):
    """Whole tracks only: the window then sits up to one track under its length."""
    m = rock(window_seconds=15 * 60)
    push(engine, m, "Fools-Overture", minutes=11)
    push(engine, m, "S1", minutes=2)
    push(engine, m, "S2", minutes=2)
    pid = (await engine.sync(m)).playlist_id
    push(engine, m, "S3", minutes=2)
    res = await engine.sync(m)
    assert res.removed == ["Fools-Overture"]
    assert apple.playlists[pid] == ["S1", "S2", "S3"]  # 6 minutes in a 15-minute window


async def test_removals_are_spread_over_syncs(engine, apple):
    """Adopting hours more than the window: 25 removals per sync, not all at once."""
    old = [f"O{i}" for i in range(35)]
    apple.playlists["p.rock"], apple.names["p.rock"] = list(old), "x"
    m = rock(playlist_id="p.rock")  # 35 one-minute tracks, 5-minute window
    first = await engine.sync(m)
    assert (
        len(first.removed) == 25 and "the rest go in the next syncs" in first.notes[0]
    )
    second = await engine.sync(m)
    assert len(second.removed) == 5 and apple.playlists["p.rock"] == old[30:]
    counts = {r["action"]: r["n"] for r in engine.store.history_counts("rock", 0)}
    assert counts == {"adopted": 1, "removed": 30}


async def test_history_records_what_changed(engine, apple, matcher):
    matcher.missing = {"B"}
    m = rock()
    push(engine, m, "A", "B")
    pid = (await engine.sync(m)).playlist_id
    apple.playlists[pid].append("X")
    await engine.sync(m)
    rows = [
        (r["action"], r["title"]) for r in reversed(engine.store.history("rock", 0))
    ]
    assert rows == [
        ("created", "Station Rock"),
        ("skipped", "B – Artist"),
        ("added", "A – Artist"),
        ("repair", "removed stray"),
    ]
