"""Matching: Spotify ID → ISRC → Apple catalog, else a strict title-and-artist search."""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.devtoken import DeveloperToken
from mcp_apple_music.matching import ARTIST_MISMATCH, NO_MATCH, Matcher, norm_title
from mcp_apple_music.spotify import SpotifyClient
from mcp_apple_music.store import Store

from .fakes import Apple, Clock

SID = "4uLU6hMCjMI75M1A2tKUQC"
SONGS = "/v1/catalog/nl/songs"
SEARCH = "/v1/catalog/nl/search"


def apple_song(id_: str, name: str, artist: str, ms: int = 200_000) -> dict:
    return {
        "id": id_,
        "type": "songs",
        "attributes": {"name": name, "artistName": artist, "durationInMillis": ms},
    }


class Spotify:
    def __init__(self) -> None:
        self.tracks: dict[str, dict] = {}
        self.down = False
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            return httpx.Response(503)
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(200, json=self.tracks[request.url.path.rsplit("/", 1)[1]])


@pytest.fixture
def apple() -> Apple:
    return Apple()


@pytest.fixture
def spotify() -> Spotify:
    sp = Spotify()
    sp.tracks[SID] = {
        "name": "Dreams - 2004 Remaster",
        "artists": [{"name": "Fleetwood Mac"}],
        "album": {"name": "Rumours"},
        "duration_ms": 257_000,
        "external_ids": {"isrc": "USWB10403681"},
    }
    return sp


@pytest.fixture
async def matcher(apple, spotify, keypair, tmp_path):
    api = AppleMusicAPI(
        DeveloperToken("T", "K", keypair[0]),
        user_token=lambda: "MUT",
        transport=httpx.MockTransport(apple),
    )
    sp = SpotifyClient("id", "secret", transport=httpx.MockTransport(spotify))
    store = Store(tmp_path / "state.db", clock=Clock())
    yield Matcher(api, store, sp)
    await api.aclose()
    await sp.aclose()
    store.close()


def entry(title="Dreams", artists="Fleetwood Mac", spotify_id=SID, **kw) -> dict:
    key = (
        f"spotify:{spotify_id}"
        if spotify_id
        else f"text:{title.lower()}|{artists.lower()}"
    )
    return {
        "mirror": "rock",
        "key": key,
        "spotify_id": spotify_id,
        "title": title,
        "artists": artists,
        "album": None,
        "duration_ms": None,
        **kw,
    }


def test_version_suffixes_and_brackets_are_ignored():
    assert norm_title("Dreams - 2004 Remaster") == norm_title("Dreams") == "dreams"
    assert norm_title("Dopamine (feat. EYLR)") == "dopamine"
    assert norm_title("Café del Mar [Live]") == "cafe del mar"


async def test_isrc_match(matcher, apple):
    apple.on("GET", SONGS, 200, {"data": [apple_song("1", "Dreams", "Fleetwood Mac")]})
    m = await matcher(entry())
    assert (m.apple_song_id, m.method, m.isrc, m.duration_ms) == (
        "1",
        "isrc",
        "USWB10403681",
        200_000,
    )
    assert apple.sent(SONGS)[0].url.params["filter[isrc]"] == "USWB10403681"


async def test_unrelated_artist_is_rejected(matcher, apple):
    apple.on("GET", SONGS, 200, {"data": []})
    apple.on(
        "GET",
        SEARCH,
        200,
        {
            "results": {
                "songs": {"data": [apple_song("9", "Dreams", "The Cranberries")]}
            }
        },
    )
    m = await matcher(entry())
    assert (m.apple_song_id, m.reason) == (None, ARTIST_MISMATCH)


async def test_no_title_match(matcher, apple):
    apple.on("GET", SONGS, 200, {"data": []})
    apple.on("GET", SEARCH, 200, {"results": {}})
    assert (await matcher(entry())).reason == NO_MATCH


async def test_ambiguous_title_takes_the_first_and_logs_the_others(
    matcher, apple, capsys
):
    apple.on("GET", SONGS, 200, {"data": []})
    candidates = [apple_song(str(i), "Dreams", "Fleetwood Mac") for i in (1, 2, 3)]
    apple.on("GET", SEARCH, 200, {"results": {"songs": {"data": candidates}}})
    m = await matcher(entry())
    assert (m.apple_song_id, m.method) == ("1", "title-ambiguous")
    (line,) = [
        json.loads(x) for x in capsys.readouterr().err.splitlines() if "ambiguous" in x
    ]
    assert line["chosen"].endswith("(1)") and len(line["alternatives"]) == 2


async def test_cache_is_reused(matcher, apple, spotify):
    apple.on("GET", SONGS, 200, {"data": [apple_song("1", "Dreams", "Fleetwood Mac")]})
    await matcher(entry())
    calls = (spotify.calls, len(apple.requests))
    assert (await matcher(entry())).apple_song_id == "1"
    assert (spotify.calls, len(apple.requests)) == calls


async def test_spotify_outage_falls_back_to_the_feed_fields(matcher, apple, spotify):
    spotify.down = True
    apple.on(
        "GET",
        SEARCH,
        200,
        {"results": {"songs": {"data": [apple_song("1", "Dreams", "Fleetwood Mac")]}}},
    )
    m = await matcher(entry())
    assert (m.apple_song_id, m.method, m.isrc) == ("1", "title", None)
    assert apple.sent(SEARCH)[0].url.params["term"] == "dreams Fleetwood Mac"
    assert apple.sent(SONGS) == []


async def test_id_only_push_is_completed_from_spotify(matcher, apple):
    apple.on("GET", SONGS, 200, {"data": [apple_song("1", "Dreams", "Fleetwood Mac")]})
    m = await matcher(entry(title="", artists=""))
    assert (m.apple_song_id, m.title, m.artists, m.album) == (
        "1",
        "Dreams - 2004 Remaster",
        "Fleetwood Mac",
        "Rumours",
    )


async def test_apple_refusing_one_query_marks_it_unavailable(matcher, apple, spotify):
    spotify.down = True
    apple.on("GET", SEARCH, 400, {"errors": [{"detail": "Invalid term"}]})
    m = await matcher(entry())
    assert m.apple_song_id is None and "Invalid term" in m.reason
