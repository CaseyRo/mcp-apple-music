"""The mirror loop's decisions, one step at a time on a fake clock."""

from __future__ import annotations

import pytest

from mcp_apple_music.mirror import SyncResult, Track
from mcp_apple_music.mirror_config import Mirror
from mcp_apple_music.rails import AppleError, RateLimited, UserTokenMissing
from mcp_apple_music.scheduler import BACKOFF_SECONDS, SIGN_IN_RETRY_SECONDS, Scheduler
from mcp_apple_music.sources import SourceError

from .fakes import Clock


class FakeStore:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def record_sync(self, name: str, error: str | None = None) -> None:
        self.errors.append(error)


class FakeEngine:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.syncs: list[bool] = []  # the removals flag of each sync
        self.raises: list[Exception] = []
        self.ingested: list[Track] = []

    def ingest(self, m, tracks, source) -> int:
        self.ingested += tracks
        return len(tracks)

    async def sync(self, m, removals: bool = True) -> SyncResult:
        self.syncs.append(removals)
        if self.raises:
            raise self.raises.pop(0)
        return SyncResult(m.name)


class FakeFeed:
    def __init__(self) -> None:
        self.responses: list = []
        self.polls = 0

    async def poll(self, mirror, source):
        self.polls += 1
        r = self.responses.pop(0) if self.responses else []
        if isinstance(r, Exception):
            raise r
        return r


ROCK = Mirror.model_validate(
    {
        "name": "rock",
        "playlist_name": "Rock",
        "reconcile_seconds": 3600,
        "source": {
            "type": "feed",
            "url": "https://radio.example/rock.json",
            "interval_seconds": 20,
        },
    }
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def loop(clock):
    s = Scheduler(FakeEngine(), FakeFeed(), [ROCK], clock=clock)
    return s


async def test_first_step_syncs_then_waits_for_the_next_poll(loop, clock):
    assert await loop.step(ROCK) == 20
    assert (loop.feed.polls, loop.engine.syncs) == (1, [True])
    clock.now += 20
    await loop.step(ROCK)  # nothing new: polled, no sync
    assert (loop.feed.polls, len(loop.engine.syncs)) == (2, 1)


async def test_new_track_from_the_feed_syncs(loop, clock):
    await loop.step(ROCK)
    loop.feed.responses = [[Track("A", "B", spotify_id="1" * 22)]]
    clock.now += 20
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2 and loop.engine.ingested[0].title == "A"


async def test_reconcile_runs_on_its_interval(loop, clock):
    await loop.step(ROCK)
    for _ in range(3600 // 20):
        clock.now += 20
        await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2


async def test_wake_syncs_at_the_next_step(loop, clock):
    await loop.step(ROCK)
    loop.wake("rock")
    clock.now += 1
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2


async def test_api_rate_limit_backs_off_ten_minutes(loop, clock):
    loop.engine.raises = [RateLimited("api", 429, "Too Many Requests")]
    await loop.step(ROCK)
    loop.wake("rock")
    clock.now += BACKOFF_SECONDS - 1
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 1  # still backing off; the feed is still polled
    assert loop.feed.polls == 2
    clock.now += 1
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2


async def test_web_rate_limit_pauses_only_removals(loop, clock):
    loop.engine.raises = [RateLimited("web", 429, "Too Many Requests")]
    await loop.step(ROCK)
    clock.now += 20
    await loop.step(ROCK)  # retried right away, without removals
    clock.now += BACKOFF_SECONDS
    loop.wake("rock")
    await loop.step(ROCK)
    assert loop.engine.syncs == [True, False, True]


async def test_missing_sign_in_retries_after_five_minutes(loop, clock):
    loop.engine.raises = [UserTokenMissing("api", None, "not signed in")]
    await loop.step(ROCK)
    clock.now += SIGN_IN_RETRY_SECONDS - 20
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 1
    clock.now += 20
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2


async def test_other_apple_errors_retry_after_a_minute(loop, clock):
    loop.engine.raises = [AppleError("api", 500, "Server error")]
    await loop.step(ROCK)
    clock.now += 60
    await loop.step(ROCK)
    assert len(loop.engine.syncs) == 2


async def test_feed_failure_is_recorded_and_polling_goes_on(loop, clock):
    loop.feed.responses = [SourceError("feed https://radio.example/rock.json: down")]
    await loop.step(ROCK)
    assert loop.engine.store.errors[0].startswith("feed: ")
    clock.now += 20
    await loop.step(ROCK)
    assert loop.feed.polls == 2


def test_spotify_mirrors_wait_for_their_source(clock, capsys):
    fav = Mirror.model_validate(
        {
            "name": "fav",
            "playlist_name": "F",
            "source": {"type": "spotify", "playlist_id": "x"},
        }
    )
    s = Scheduler(FakeEngine(), FakeFeed(), [ROCK, fav], clock=clock)
    assert [m.name for m in s.mirrors] == ["rock"]
    assert "source_unsupported" in capsys.readouterr().err


async def test_loop_starts_once(loop):
    loop._running = True  # as if the lifespan had already started it
    await loop.run()  # returns at once instead of polling a second time
    assert loop.feed.polls == 0
