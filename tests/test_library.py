"""Writes are verified by read-back: a 2xx is not a success until the playlist agrees."""

from __future__ import annotations

import time

import httpx
import pytest

from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.apple_web import AppleWebPlayer
from mcp_apple_music.devtoken import DeveloperToken
from mcp_apple_music.library import NOT_ADDED, NOT_REMOVED, Library
from mcp_apple_music.rails import AppleError

from .fakes import Apple, serve_web_player, web_token

TRACKS = "/v1/me/library/playlists/p.1/tracks"


def lib_track(library_id: str, catalog: str) -> dict:
    """A playlist track as the documented rail lists it."""
    return {
        "id": library_id,
        "type": "library-songs",
        "attributes": {
            "playParams": {"id": library_id, "isLibrary": True, "catalogId": catalog}
        },
    }


def listing(*tracks: dict) -> dict:
    return {"data": list(tracks)}


def web_listing(*ids: str) -> dict:
    return {"data": [{"id": i, "type": "library-songs"} for i in ids]}


@pytest.fixture
def apple() -> Apple:
    a = Apple()
    serve_web_player(a, web_token(int(time.time()) + 70 * 86_400))
    return a


@pytest.fixture
async def library(apple, keypair):
    transport = httpx.MockTransport(apple)
    api = AppleMusicAPI(
        DeveloperToken("T", "K", keypair[0]), lambda: "MUT", transport=transport
    )
    web = AppleWebPlayer(lambda: "MUT", transport=transport)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    lib = Library(api, web, sleep=sleep)
    lib.slept = slept
    yield lib
    await api.aclose()
    await web.aclose()


# -- adds ---------------------------------------------------------------------


async def test_add_confirmed(library, apple):
    apple.on("GET", TRACKS, 404, {"errors": []})  # empty playlist before
    apple.on(
        "GET", TRACKS, 200, listing(lib_track("i.1", "s1"), lib_track("i.2", "s2"))
    )
    apple.on("POST", TRACKS, 204)
    result = await library.add_tracks("p.1", ["s1", "s2"])
    assert (result.rail, result.ok, result.reason, result.failed) == (
        "api",
        True,
        None,
        [],
    )
    assert len(result.tracks) == 2 and library.slept == []


async def test_add_listed_under_the_librarys_own_copy(library, apple):
    """Live 2026-10-29: catalog 185868578 went in as the library's 323489066, the
    same recording from another album. The playlist grew by one: that's the add."""
    apple.on("GET", TRACKS, 200, listing(lib_track("i.1", "s1")))
    apple.on(
        "GET",
        TRACKS,
        200,
        listing(lib_track("i.1", "s1"), lib_track("i.EY", "323489066")),
    )
    apple.on("POST", TRACKS, 204)
    result = await library.add_tracks("p.1", ["185868578"])
    assert (result.ok, result.listed) == (True, ["323489066"])


async def test_add_2xx_but_absent_is_a_failure(library, apple):
    apple.on("GET", TRACKS, 200, listing(lib_track("i.1", "s1")))
    apple.on("POST", TRACKS, 204)
    result = await library.add_tracks("p.1", ["s2"])
    assert (result.ok, result.reason, result.failed) == (False, NOT_ADDED, ["s2"])
    assert library.slept == [1.0, 2.0, 4.0]  # retried for lag before giving up
    assert [t["id"] for t in result.tracks] == ["i.1"]


async def test_add_survives_read_lag(library, apple):
    apple.on("GET", TRACKS, 404, {"errors": []})
    apple.on("GET", TRACKS, 404, {"errors": []})  # first read-back: not there yet
    apple.on("GET", TRACKS, 200, listing(lib_track("i.1", "s1")))
    apple.on("POST", TRACKS, 204)
    result = await library.add_tracks("p.1", ["s1"])
    assert result.ok and library.slept == [1.0]


async def test_add_of_a_repeat_needs_a_second_copy(library, apple):
    """A heavy-rotation song is already in the window: one copy is not proof."""
    apple.on("GET", TRACKS, 200, listing(lib_track("i.1", "s1")))
    apple.on("POST", TRACKS, 204)
    assert not (await library.add_tracks("p.1", ["s1"])).ok

    apple.routes[("GET", TRACKS)] = [
        httpx.Response(200, json=listing(lib_track("i.1", "s1"))),
        httpx.Response(
            200, json=listing(lib_track("i.1", "s1"), lib_track("i.1", "s1"))
        ),
    ]
    assert (await library.add_tracks("p.1", ["s1"])).ok


async def test_accepted_add_with_failed_read_back_is_unverified(library, apple):
    apple.on("GET", TRACKS, 404, {"errors": []})
    apple.on("GET", TRACKS, 429, {})
    apple.on("POST", TRACKS, 204)
    result = await library.add_tracks("p.1", ["s1"])
    assert not result.ok and result.tracks is None
    assert result.reason.startswith("accepted by Apple, but the read-back failed")


async def test_rejected_add_raises_before_any_claim(library, apple):
    apple.on("GET", TRACKS, 404, {"errors": []})
    apple.on("POST", TRACKS, 400, {"errors": [{"detail": "Invalid song id"}]})
    with pytest.raises(AppleError, match="Invalid song id"):
        await library.add_tracks("p.1", ["bogus"])


# -- removals -----------------------------------------------------------------


async def test_remove_confirmed(library, apple):
    apple.on("GET", TRACKS, 200, web_listing("i.A", "i.B"))
    apple.on("GET", TRACKS, 200, web_listing("i.B"))
    apple.on("DELETE", TRACKS, 204)
    result = await library.remove_tracks("p.1", ["i.A"])
    assert (result.rail, result.ok) == ("web", True)
    assert apple.sent(TRACKS)[1].method == "DELETE"
    assert apple.sent(TRACKS)[1].url.host == "amp-api.music.apple.com"


async def test_remove_2xx_but_present_is_a_failure(library, apple):
    apple.on("GET", TRACKS, 200, web_listing("i.A", "i.B"))
    apple.on("DELETE", TRACKS, 204)
    result = await library.remove_tracks("p.1", ["i.A"])
    assert (result.ok, result.reason, result.failed) == (False, NOT_REMOVED, ["i.A"])


async def test_removing_two_copies_sends_two_removals(library, apple):
    """Copies share one ID; each listed ID removes one copy, the topmost."""
    apple.on("GET", TRACKS, 200, web_listing("i.A", "i.B", "i.A"))
    apple.on("GET", TRACKS, 200, web_listing("i.B"))
    apple.on("DELETE", TRACKS, 204)
    result = await library.remove_tracks("p.1", ["i.A", "i.A"])
    deletes = [r for r in apple.sent(TRACKS) if r.method == "DELETE"]
    assert result.ok and len(deletes) == 2
    assert {r.url.params["mode"] for r in deletes} == {"first"}


async def test_over_removal_is_visible_in_the_read_back(library, apple):
    """If Apple ever removed more than asked, the caller sees the playlist as it is."""
    apple.on("GET", TRACKS, 200, web_listing("i.A", "i.A", "i.B"))
    apple.on("GET", TRACKS, 200, web_listing("i.B"))
    apple.on("DELETE", TRACKS, 204)
    result = await library.remove_tracks("p.1", ["i.A"])
    assert result.ok and [t["id"] for t in result.tracks] == ["i.B"]


async def test_first_removal_rejected_raises(library, apple):
    apple.on("GET", TRACKS, 200, web_listing("i.A"))
    apple.on("DELETE", TRACKS, 404, {"errors": [{"detail": "Not found"}]})
    with pytest.raises(AppleError, match="Not found"):
        await library.remove_tracks("p.1", ["i.A"])


async def test_partial_removal_reports_what_happened(library, apple):
    apple.on("GET", TRACKS, 200, web_listing("i.A", "i.B"))
    apple.on("GET", TRACKS, 200, web_listing("i.B"))
    apple.on("DELETE", TRACKS, 204)
    apple.on("DELETE", TRACKS, 500, {"errors": [{"detail": "Upstream error"}]})
    result = await library.remove_tracks("p.1", ["i.A", "i.B"])
    assert not result.ok and result.failed == ["i.B"] and result.reason == NOT_REMOVED
    assert result.stopped == "web rail: HTTP 500: Upstream error"
