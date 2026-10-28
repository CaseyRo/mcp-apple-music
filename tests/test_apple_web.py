"""The web-player rail against a scripted music.apple.com and amp-api."""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.apple_web import (
    REFRESH_BEFORE_SECONDS,
    AppleWebPlayer,
    extract_web_token,
    playlist_kind,
)
from mcp_apple_music.devtoken import DeveloperToken
from mcp_apple_music.rails import (
    RailUnavailable,
    RateLimited,
    UserTokenMissing,
    UserTokenRejected,
)

from .fakes import BUNDLE, Apple, Clock, serve_web_player, web_token

TRACKS = "/v1/me/library/playlists/p.1/tracks"


@pytest.fixture
def apple() -> Apple:
    return Apple()


@pytest.fixture
def clock() -> Clock:
    c = Clock()
    c.now = 1_800_000_000
    return c


@pytest.fixture
def user() -> dict:
    return {"token": "MUT", "rejected": 0}


@pytest.fixture
async def web(apple, clock, user):
    def rejected() -> None:
        user["rejected"] += 1

    w = AppleWebPlayer(
        user_token=lambda: user["token"],
        on_user_token_rejected=rejected,
        transport=httpx.MockTransport(apple),
        clock=clock,
    )
    yield w
    await w.aclose()


def test_extract_from_bundle_snippet():
    tok = web_token(1_900_000_000)
    assert extract_web_token(f'x="{tok}",y="eyJhbGciOiJub25lIn0.e30."') == tok
    with pytest.raises(ValueError):
        extract_web_token('const qa="eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJvdGhlciJ9.c2ln"')


async def test_requests_carry_web_token_user_token_and_origin(web, apple, clock):
    tok = web_token(int(clock.now) + 70 * 86_400)
    serve_web_player(apple, tok)
    apple.on("GET", TRACKS, 200, {"data": [{"id": "i.A"}, {"id": "i.B"}]})
    read = await web.playlist_tracks("p.1")
    await web.playlist_tracks("p.1")

    assert [t["id"] for t in read.data] == ["i.A", "i.B"]
    req = apple.sent(TRACKS)[0]
    assert req.url.host == "amp-api.music.apple.com"
    assert req.headers["Authorization"] == f"Bearer {tok}"
    assert req.headers["Music-User-Token"] == "MUT"
    assert req.headers["Origin"] == "https://music.apple.com"
    assert len(apple.sent(BUNDLE)) == 1  # harvested once, then cached


async def test_token_refreshed_before_expiry(web, apple, clock):
    exp = int(clock.now) + 70 * 86_400
    serve_web_player(apple, web_token(exp, n=1))
    apple.on("GET", TRACKS, 200, {"data": []})
    await web.playlist_tracks("p.1")

    apple.routes[("GET", BUNDLE)] = [
        httpx.Response(200, text=f'a="{web_token(exp + 10**6, n=2)}"')
    ]
    clock.now = exp - REFRESH_BEFORE_SECONDS  # refresh window opens
    await web.playlist_tracks("p.1")
    assert len(apple.sent(BUNDLE)) == 2
    first, second = (r.headers["Authorization"] for r in apple.sent(TRACKS))
    assert first != second


async def test_failed_refresh_keeps_a_still_valid_token(web, apple, clock):
    exp = int(clock.now) + 70 * 86_400
    tok = web_token(exp)
    serve_web_player(apple, tok)
    apple.on("GET", TRACKS, 200, {"data": []})
    await web.playlist_tracks("p.1")

    apple.routes[("GET", "/us/new")] = [httpx.Response(503)]
    clock.now = exp - 60  # in the refresh window, not yet expired
    await web.playlist_tracks("p.1")
    assert apple.sent(TRACKS)[-1].headers["Authorization"] == f"Bearer {tok}"


@pytest.mark.parametrize(
    "page",
    [
        httpx.Response(503),
        httpx.Response(200, text="<html>no bundle here</html>"),
    ],
    ids=["site-down", "layout-changed"],
)
async def test_rail_unavailable_without_token(web, apple, keypair, page):
    apple.on(
        "GET", "/us/browse", 301, headers={"Location": "https://music.apple.com/us/new"}
    )
    apple.routes[("GET", "/us/new")] = [page]
    for call in (
        web.remove_track("p.1", "i.A"),
        web.rename_playlist("p.1", "x"),
        web.delete_playlist("p.1"),
    ):
        with pytest.raises(RailUnavailable) as e:
            await call
        assert e.value.rail == "web"
    assert not apple.sent(TRACKS) and not apple.sent("/v1/me/library/playlists/p.1")

    # The documented rail keeps working on its own token.
    apple.on(
        "GET",
        "/v1/catalog/nl/search",
        200,
        {"results": {"songs": {"data": [{"id": "s1"}]}}},
    )
    api = AppleMusicAPI(
        DeveloperToken("T", "K", keypair[0]),
        user_token=lambda: "MUT",
        transport=httpx.MockTransport(apple),
    )
    assert [s["id"] for s in (await api.search_catalog("x")).data] == ["s1"]
    await api.aclose()


async def test_401_reharvests_once_and_retries(web, apple, clock):
    exp = int(clock.now) + 70 * 86_400
    serve_web_player(apple, web_token(exp, n=1))
    apple.on(
        "PATCH",
        "/v1/me/library/playlists/p.1",
        401,
        {"errors": [{"title": "Unauthorized"}]},
    )
    apple.on("PATCH", "/v1/me/library/playlists/p.1", 204)
    apple.routes[("GET", BUNDLE)].append(
        httpx.Response(200, text=f'a="{web_token(exp, n=2)}"')
    )

    result = await web.rename_playlist("p.1", "ROCK")
    assert (result.rail, result.status) == ("web", 204)
    assert len(apple.sent(BUNDLE)) == 2


async def test_persistent_401_rejects_user_token(web, apple, clock, user):
    serve_web_player(apple, web_token(int(clock.now) + 70 * 86_400))
    apple.on(
        "DELETE",
        "/v1/me/library/playlists/p.1",
        401,
        {"errors": [{"title": "Unauthorized"}]},
    )
    with pytest.raises(UserTokenRejected) as e:
        await web.delete_playlist("p.1")
    assert e.value.rail == "web" and user["rejected"] == 1
    assert len(apple.sent("/v1/me/library/playlists/p.1")) == 2  # one retry, no more


async def test_writes_send_the_web_player_calls(web, apple, clock):
    serve_web_player(apple, web_token(int(clock.now) + 70 * 86_400))
    apple.on("DELETE", TRACKS, 204)
    apple.on("PATCH", "/v1/me/library/playlists/p.1", 204)
    apple.on("DELETE", "/v1/me/library/playlists/p.1", 204)

    removed = await web.remove_track("p.1", "i.A")
    renamed = await web.rename_playlist("p.1", "ROCK 24")
    deleted = await web.delete_playlist("p.1")

    assert {(w.rail, w.status) for w in (removed, renamed, deleted)} == {("web", 204)}
    params = apple.sent(TRACKS)[0].url.params
    assert (params["ids[library-songs]"], params["mode"]) == ("i.A", "first")
    await web.remove_track("p.1", "i.A", every_copy=True)
    assert apple.sent(TRACKS)[-1].url.params["mode"] == "all"
    patch = next(r for r in apple.requests if r.method == "PATCH")
    assert json.loads(patch.content) == {"attributes": {"name": "ROCK 24"}}


async def test_web_429_is_its_own_rail(web, apple, clock):
    serve_web_player(apple, web_token(int(clock.now) + 70 * 86_400))
    apple.on("DELETE", TRACKS, 429, {})
    with pytest.raises(RateLimited) as e:
        await web.remove_track("p.1", "i.A")
    assert e.value.rail == "web" and web.throttle.recent()


async def test_no_user_token_sends_nothing(web, apple, user):
    user["token"] = None
    with pytest.raises(UserTokenMissing):
        await web.delete_playlist("p.1")
    assert apple.requests == []


@pytest.mark.parametrize(
    ("attributes", "kind"),
    [
        ({"canEdit": True, "playParams": {"globalId": "pl.u-abc"}}, "api"),
        ({"canEdit": False, "playParams": {"globalId": "pl.u-abc"}}, "user"),
        ({"canEdit": False, "playParams": {}}, "user"),
        (
            {
                "canEdit": False,
                "playParams": {"globalId": "pl.f4d106fed2bd41149aaacabb233eb5eb"},
            },
            "apple",
        ),
        ({}, "user"),
    ],
)
def test_playlist_kind(attributes, kind):
    assert playlist_kind({"id": "p.1", "attributes": attributes}) == kind
