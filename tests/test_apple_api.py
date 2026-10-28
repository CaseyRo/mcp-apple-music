"""The documented rail against a scripted api.music.apple.com (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.devtoken import DeveloperToken
from mcp_apple_music.rails import (
    THROTTLE_WINDOW_SECONDS,
    AppleError,
    RateLimited,
    Throttle,
    UserTokenMissing,
    UserTokenRejected,
)

from .fakes import Apple, Clock


def song(id_: str, isrc: str = "") -> dict:
    return {"id": id_, "type": "songs", "attributes": {"name": id_, "isrc": isrc}}


@pytest.fixture
def apple() -> Apple:
    return Apple()


@pytest.fixture
def user() -> dict:
    return {"token": "MUT", "rejected": 0}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(apple, user, clock, keypair):
    def rejected() -> None:
        user["rejected"] += 1

    c = AppleMusicAPI(
        DeveloperToken("TEAM", "KEY", keypair[0]),
        user_token=lambda: user["token"],
        on_user_token_rejected=rejected,
        transport=httpx.MockTransport(apple),
        throttle=Throttle(clock),
    )
    yield c
    await c.aclose()


async def test_headers_and_storefront_cached(client, apple):
    apple.on("GET", "/v1/catalog/nl/search", 200, {"results": {}})
    await client.search_catalog("x")
    await client.search_catalog("y")
    assert len(apple.sent("/v1/me/storefront")) == 1
    req = apple.sent("/v1/me/storefront")[0]
    assert req.headers["Authorization"].startswith("Bearer ey")
    assert req.headers["Music-User-Token"] == "MUT"
    # Catalog search is not user-scoped: no user token travels with it.
    assert "Music-User-Token" not in apple.sent("/v1/catalog/nl/search")[0].headers


async def test_search_flattens_types_and_caps_limit(client, apple):
    apple.on(
        "GET",
        "/v1/catalog/nl/search",
        200,
        {
            "results": {
                "songs": {"data": [song("s1")]},
                "albums": {"data": [{"id": "a1", "type": "albums"}]},
            }
        },
    )
    read = await client.search_catalog("eylr", types=("songs", "albums"), limit=50)
    assert [r["id"] for r in read.data] == ["s1", "a1"]
    assert not read.possibly_throttled
    params = apple.sent("/v1/catalog/nl/search")[0].url.params
    assert params["types"] == "songs,albums" and params["limit"] == "25"


async def test_library_search_is_user_scoped(client, apple):
    apple.on(
        "GET",
        "/v1/me/library/search",
        200,
        {"results": {"library-songs": {"data": [{"id": "i.1"}]}}},
    )
    read = await client.search_library("x")
    assert [r["id"] for r in read.data] == ["i.1"]
    assert apple.sent("/v1/me/library/search")[0].headers["Music-User-Token"] == "MUT"


async def test_isrc_lookup_batches_by_25(client, apple):
    isrcs = [f"NLA{n:09d}" for n in range(30)]
    apple.on("GET", "/v1/catalog/nl/songs", 200, {"data": [song("s1", isrcs[0])]})
    apple.on("GET", "/v1/catalog/nl/songs", 200, {"data": []})
    read = await client.songs_by_isrc(isrcs)
    sent = [
        r.url.params["filter[isrc]"].split(",")
        for r in apple.sent("/v1/catalog/nl/songs")
    ]
    assert [len(b) for b in sent] == [25, 5]
    assert sent[0] + sent[1] == isrcs
    assert [s["id"] for s in read.data] == ["s1"]


async def test_playlists_follow_next_links(client, apple):
    path = "/v1/me/library/playlists"
    apple.on("GET", path, 200, {"data": [{"id": "p.1"}], "next": f"{path}?offset=100"})
    apple.on("GET", path, 200, {"data": [{"id": "p.2"}]})
    read = await client.library_playlists()
    assert [p["id"] for p in read.data] == ["p.1", "p.2"]
    assert [str(r.url.query, "ascii") for r in apple.sent(path)] == [
        "limit=100",
        "offset=100",
    ]


async def test_empty_playlist_404_is_empty_not_error(client, apple):
    apple.on("GET", "/v1/me/library/playlists/p.1/tracks", 404, {"errors": []})
    read = await client.playlist_tracks("p.1")
    assert read.data == [] and not read.possibly_throttled


async def test_missing_playlist_is_an_error(client, apple):
    apple.on(
        "GET",
        "/v1/me/library/playlists/p.x",
        404,
        {"errors": [{"title": "Resource Not Found", "detail": "No such playlist"}]},
    )
    with pytest.raises(AppleError) as e:
        await client.library_playlist("p.x")
    assert (e.value.rail, e.value.status, e.value.reason) == (
        "api",
        404,
        "No such playlist",
    )


async def test_create_and_append_name_the_rail(client, apple):
    apple.on("POST", "/v1/me/library/playlists", 201, {"data": [{"id": "p.new"}]})
    apple.on("POST", "/v1/me/library/playlists/p.new/tracks", 204)
    created = await client.create_playlist("ROCK", "rolling 24h")
    appended = await client.append_tracks("p.new", ["s1", "s2"])
    assert (created.rail, created.status, created.id) == ("api", 201, "p.new")
    assert (appended.rail, appended.status) == ("api", 204)
    assert json.loads(apple.sent("/v1/me/library/playlists")[0].content) == {
        "attributes": {"name": "ROCK", "description": "rolling 24h"}
    }
    assert json.loads(
        apple.sent("/v1/me/library/playlists/p.new/tracks")[0].content
    ) == {"data": [{"id": "s1", "type": "songs"}, {"id": "s2", "type": "songs"}]}


async def test_rejected_write_reports_rail_status_and_reason(client, apple):
    apple.on(
        "POST",
        "/v1/me/library/playlists/p.1/tracks",
        400,
        {"errors": [{"detail": "Invalid song id"}]},
    )
    with pytest.raises(AppleError, match=r"api rail: HTTP 400: Invalid song id"):
        await client.append_tracks("p.1", ["bogus"])


@pytest.mark.parametrize("status", [401, 403])
async def test_user_token_rejected_marks_it(client, apple, user, status):
    apple.on(
        "GET",
        "/v1/me/library/playlists",
        status,
        {"errors": [{"title": "Unauthorized"}]},
    )
    with pytest.raises(UserTokenRejected) as e:
        await client.library_playlists()
    assert e.value.status == status and "sign in again" in e.value.reason
    assert user["rejected"] == 1


async def test_catalog_401_does_not_blame_user_token(client, apple, user):
    await client.storefront()
    apple.on(
        "GET", "/v1/catalog/nl/search", 401, {"errors": [{"title": "Unauthorized"}]}
    )
    with pytest.raises(AppleError) as e:
        await client.search_catalog("x")
    assert type(e.value) is AppleError and user["rejected"] == 0


async def test_no_user_token_fails_fast(client, apple, user):
    user["token"] = None
    with pytest.raises(UserTokenMissing):
        await client.create_playlist("x")
    assert apple.requests == []


async def test_429_is_rate_limited_and_labels_later_empties(client, apple, clock):
    path = "/v1/catalog/nl/search"
    apple.on("GET", path, 429, {"errors": [{"title": "Too Many Requests"}]})
    apple.on("GET", path, 200, {"results": {}})
    apple.on("GET", path, 200, {"results": {"songs": {"data": [song("s1")]}}})
    apple.on("GET", path, 200, {"results": {}})

    with pytest.raises(RateLimited) as e:
        await client.search_catalog("x")
    assert "not found" not in str(e.value).lower()

    assert (await client.search_catalog("x")).possibly_throttled  # empty, inside window
    assert not (await client.search_catalog("x")).possibly_throttled  # real answer
    assert not (await client.search_catalog("x")).possibly_throttled  # cleared by it


async def test_throttle_label_expires_with_window(client, apple, clock):
    path = "/v1/catalog/nl/search"
    apple.on("GET", path, 429, {})
    apple.on("GET", path, 200, {"results": {}})
    with pytest.raises(RateLimited):
        await client.search_catalog("x")
    clock.now += THROTTLE_WINDOW_SECONDS
    assert not (await client.search_catalog("x")).possibly_throttled


async def test_network_failure_is_an_apple_error(keypair):
    def boom(request):
        raise httpx.ConnectError("down")

    c = AppleMusicAPI(
        DeveloperToken("T", "K", keypair[0]),
        user_token=lambda: "MUT",
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(AppleError) as e:
        await c.storefront()
    assert e.value.status is None and "request failed" in e.value.reason
    await c.aclose()


async def test_request_uses_renewed_developer_token(apple, keypair):
    now = [1_800_000_000]
    dev = DeveloperToken(
        "T",
        "K",
        keypair[0],
        ttl_seconds=1000,
        renew_before_seconds=100,
        clock=lambda: now[0],
    )
    c = AppleMusicAPI(
        dev, user_token=lambda: "MUT", transport=httpx.MockTransport(apple)
    )
    apple.on("GET", "/v1/catalog/nl/search", 200, {"results": {}})
    await c.search_catalog("x")
    now[0] += 950  # inside the renewal window
    await c.search_catalog("y")
    first, second = (
        r.headers["Authorization"] for r in apple.sent("/v1/catalog/nl/search")
    )
    assert first != second
    await c.aclose()
