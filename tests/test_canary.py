"""The canary routine against scripted rails: pass, fail and cleanup reporting."""

from __future__ import annotations

import time

import httpx
import pytest

from mcp_apple_music import canary
from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.apple_web import AppleWebPlayer
from mcp_apple_music.devtoken import DeveloperToken
from mcp_apple_music.library import Library

from .fakes import Apple, serve_web_player, web_token

LISTS = "/v1/me/library/playlists"
TRACKS = f"{LISTS}/p.c/tracks"


def copy(n: int) -> dict:
    """One occurrence of the probe song, as either rail lists it."""
    return {"id": f"i.{n}", "attributes": {"playParams": {"catalogId": "s1"}}}


@pytest.fixture
def apple() -> Apple:
    a = Apple()
    serve_web_player(a, web_token(int(time.time()) + 70 * 86_400))
    a.on(
        "GET",
        "/v1/catalog/nl/search",
        200,
        {"results": {"songs": {"data": [{"id": "s1"}]}}},
    )
    a.on("POST", LISTS, 201, {"data": [{"id": "p.c"}]})
    a.on("POST", TRACKS, 204)
    a.on("DELETE", TRACKS, 204)
    a.on("DELETE", f"{LISTS}/p.c", 204)
    return a


@pytest.fixture
async def library(apple, keypair):
    transport = httpx.MockTransport(apple)
    api = AppleMusicAPI(
        DeveloperToken("T", "K", keypair[0]), lambda: "MUT", transport=transport
    )
    web = AppleWebPlayer(lambda: "MUT", transport=transport)

    async def no_sleep(_: float) -> None:
        pass

    yield Library(api, web, sleep=no_sleep)
    await api.aclose()
    await web.aclose()


def tracks(*responses) -> list[httpx.Response]:
    return [
        httpx.Response(404, json={"errors": []})
        if r is None
        else httpx.Response(200, json={"data": r})
        for r in responses
    ]


async def test_pass_records_copies_left(library, apple):
    # before add, after add, web listing, before remove, after remove
    apple.routes[("GET", TRACKS)] = tracks(
        None, [copy(1), copy(2)], [copy(1), copy(2)], [copy(1), copy(2)], [copy(2)]
    )
    result = await canary.run(library)
    assert result.ok and result.error is None
    assert result.copies_left == 1
    assert result.steps[-1] == "deleted p.c (web)"


async def test_removal_of_every_copy_fails_the_canary(library, apple):
    """The window relies on mode=first taking one copy (D6); taking both is a break."""
    shared = [copy(1), copy(1)]
    apple.routes[("GET", TRACKS)] = tracks(None, shared, shared, shared, None)
    result = await canary.run(library)
    assert not result.ok and result.copies_left == 0
    assert result.error == "remove: expected 1 of 2 copies left, found 0"
    assert result.steps[-1] == "deleted p.c (web)"


async def test_failed_append_still_cleans_up(library, apple):
    apple.routes[("GET", TRACKS)] = tracks(None)  # the songs never show up
    result = await canary.run(library)
    assert not result.ok and result.error == "append: not present after write"
    assert apple.sent(f"{LISTS}/p.c")[-1].method == "DELETE"


async def test_failed_cleanup_fails_the_canary(library, apple):
    apple.routes[("GET", TRACKS)] = tracks(
        None, [copy(1), copy(2)], [copy(1), copy(2)], [copy(1), copy(2)], [copy(2)]
    )
    apple.routes[("DELETE", f"{LISTS}/p.c")] = [
        httpx.Response(500, json={"errors": [{"detail": "boom"}]})
    ]
    result = await canary.run(library)
    assert not result.ok and result.error.startswith("cleanup:")
    assert "could not delete scratch playlist p.c" in result.steps[-1]


async def test_web_rail_down_is_reported(library, apple):
    apple.routes[("GET", "/us/new")] = [httpx.Response(503)]
    apple.routes[("GET", TRACKS)] = tracks(None, [copy(1), copy(2)])
    result = await canary.run(library)
    assert not result.ok and "web-player token unavailable" in result.error
