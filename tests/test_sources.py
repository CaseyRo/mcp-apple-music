"""Payload shapes and the polled feed."""

from __future__ import annotations

import httpx
import pytest

from mcp_apple_music.mirror_config import FeedSource
from mcp_apple_music.sources import Feed, SourceError, track_from_payload
from mcp_apple_music.store import Store

from .fakes import Clock

SID = "4uLU6hMCjMI75M1A2tKUQC"
URL = "https://radio.example/api/rock.json"


def item(n: int) -> dict:
    return {
        "id": f"{n:022d}",
        "name": f"Song {n}",
        "artists": "A",
        "duration_ms": 200_000,
    }


@pytest.mark.parametrize(
    "payload, expected",
    [
        (  # feed shape
            {
                "id": SID,
                "duration_ms": 245000,
                "name": "Dreams",
                "artists": "Fleetwood Mac",
                "album": "Rumours",
            },
            ("Dreams", "Fleetwood Mac", "Rumours", SID, 245_000),
        ),
        (  # webhook shape, duration in seconds
            {
                "playlist": "rock",
                "id": SID,
                "name": "Dreams",
                "artists": "Fleetwood Mac",
                "album": "Rumours",
                "duration": "245",
            },
            ("Dreams", "Fleetwood Mac", "Rumours", SID, 245_000),
        ),
        (  # webhook shape, duration in milliseconds
            {
                "playlist": "rock",
                "id": SID,
                "name": "Dreams",
                "artists": "Fleetwood Mac",
                "duration": 245000,
            },
            ("Dreams", "Fleetwood Mac", None, SID, 245_000),
        ),
        (  # the 2025 example: no ID, no duration
            {
                "playlist": "music",
                "name": "The Fez",
                "artists": "Steely Dan",
                "album": "The Royal Scam",
            },
            ("The Fez", "Steely Dan", "The Royal Scam", None, None),
        ),
        ({"id": SID}, ("", "", None, SID, None)),  # ID only: completed from Spotify
        ({"name": "X", "artists": ["A", "B"]}, ("X", "A, B", None, None, None)),
    ],
)
def test_both_payload_shapes(payload, expected):
    t = track_from_payload(payload)
    assert (t.title, t.artists, t.album, t.spotify_id, t.duration_ms) == expected


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"id": "../me/playlists"}, "not a Spotify track ID"),
        ({"name": "No artists"}, "needs an `id`"),
        ({"id": SID, "duration": "long"}, "must be a number"),
        ({"id": SID, "name": {"x": 1}}, "must be text"),
        ([], "JSON object"),
    ],
)
def test_unusable_payloads(payload, message):
    with pytest.raises(ValueError, match=message):
        track_from_payload(payload)


class Server:
    def __init__(self) -> None:
        self.responses: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
async def feed(tmp_path):
    server = Server()
    store = Store(tmp_path / "state.db", clock=Clock())
    f = Feed(store, transport=httpx.MockTransport(server))
    f.server = server
    yield f
    await f.aclose()
    store.close()


SOURCE = FeedSource(type="feed", url=URL)


async def test_single_object_feed(feed):
    feed.server.responses = [
        httpx.Response(200, json=item(1)),
        httpx.Response(200, json=item(2)),
    ]
    assert [t.title for t in await feed.poll("rock", SOURCE)] == ["Song 1"]
    assert [t.title for t in await feed.poll("rock", SOURCE)] == ["Song 2"]


async def test_unchanged_feed_adds_nothing(feed):
    feed.server.responses = [httpx.Response(200, json=item(1))]
    await feed.poll("rock", SOURCE)
    assert await feed.poll("rock", SOURCE) == []


async def test_list_feed_is_lossless(feed):
    feed.server.responses = [
        httpx.Response(200, json=[item(n) for n in range(1, 11)]),
        httpx.Response(200, json=[item(n) for n in range(4, 14)]),
    ]
    await feed.poll("rock", SOURCE)
    assert [t.title for t in await feed.poll("rock", SOURCE)] == [
        "Song 11",
        "Song 12",
        "Song 13",
    ]


async def test_unreachable_feed_keeps_the_cursor(feed):
    feed.server.responses = [httpx.Response(200, json=item(1))]
    await feed.poll("rock", SOURCE)
    feed.server.responses = [httpx.ConnectError("down")]
    with pytest.raises(SourceError, match="down"):
        await feed.poll("rock", SOURCE)
    feed.server.responses = [httpx.Response(200, json=item(1))]
    assert await feed.poll("rock", SOURCE) == []  # still compared against Song 1


async def test_redirects_are_followed_each_time(feed):
    feed.server.responses = [
        httpx.Response(
            301, headers={"Location": "https://radio.example/2026/rock.json"}
        ),
        httpx.Response(200, json=item(1)),
    ]
    assert [t.title for t in await feed.poll("rock", SOURCE)] == ["Song 1"]
