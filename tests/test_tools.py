"""The agent tools over an in-memory MCP client, against scripted Apple rails."""

from __future__ import annotations


import json
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import mcp_apple_music.server as srv
from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.apple_web import AppleWebPlayer
from mcp_apple_music.config import settings
from mcp_apple_music.library import Library
from mcp_apple_music.store import APPLE_USER_TOKEN

from .fakes import Apple, serve_web_player, web_token

SEARCH = "/v1/catalog/nl/search"
LISTS = "/v1/me/library/playlists"
MINE = f"{LISTS}/p.mine"
TRACKS = f"{MINE}/tracks"


def cat_song(id_, title, artist, album="Album", isrc="", ms=200_000) -> dict:
    return {
        "id": id_,
        "type": "songs",
        "attributes": {
            "name": title,
            "artistName": artist,
            "albumName": album,
            "isrc": isrc,
            "durationInMillis": ms,
        },
    }


def lib_song(lib_id, catalog, title="Song", artist="Artist") -> dict:
    return {
        "id": lib_id,
        "type": "library-songs",
        "attributes": {
            "name": title,
            "artistName": artist,
            "albumName": "Album",
            "durationInMillis": 180_000,
            "playParams": {"id": lib_id, "isLibrary": True, "catalogId": catalog},
        },
    }


def playlist(id_, name, can_edit=True, count=None, global_id="") -> dict:
    attrs = {"name": name, "canEdit": can_edit, "playParams": {"globalId": global_id}}
    if count is not None:
        attrs["trackCount"] = count
    return {"id": id_, "type": "library-playlists", "attributes": attrs}


@pytest.fixture
def apple() -> Apple:
    a = Apple()
    serve_web_player(a, web_token(int(time.time()) + 70 * 86_400))
    return a


@pytest.fixture
async def rails(apple, keypair, monkeypatch):
    monkeypatch.setattr(settings, "apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "apple_key_id", "KEY")
    monkeypatch.setattr(
        settings, "apple_private_key", type(settings.apple_private_key)(keypair[0])
    )
    transport = httpx.MockTransport(apple)

    def mut() -> str | None:
        return srv.store().valid_token(APPLE_USER_TOKEN)

    def rejected() -> None:
        srv.store().mark_token_invalid(APPLE_USER_TOKEN)

    api = AppleMusicAPI(srv.developer_token(), mut, rejected, transport=transport)
    web = AppleWebPlayer(mut, rejected, transport=transport)

    async def no_sleep(_: float) -> None:
        pass

    lib = Library(api, web, sleep=no_sleep)
    monkeypatch.setattr(srv, "apple_api", lambda: api)
    monkeypatch.setattr(srv, "apple_web", lambda: web)
    monkeypatch.setattr(srv, "library", lambda: lib)
    srv.store().set_token(APPLE_USER_TOKEN, "MUT")
    yield apple
    await api.aclose()
    await web.aclose()


async def call(name: str, args: dict | None = None) -> dict:
    async with Client(srv.mcp) as client:
        return (await client.call_tool(name, args or {})).structured_content


def writes(apple: Apple) -> list[httpx.Request]:
    return [r for r in apple.requests if r.method in ("POST", "PATCH", "DELETE")]


# -- search -------------------------------------------------------------------


async def test_featured_artist_filter(rails):
    rails.on(
        "GET",
        SEARCH,
        200,
        {
            "results": {
                "songs": {
                    "data": [
                        cat_song(
                            "1",
                            "Dopamine (feat. EYLR)",
                            "Purple Disco Machine",
                            "Exotica",
                            "AUDCB1701788",
                        ),
                        cat_song(
                            "2",
                            "Hypnotized",
                            "Purple Disco Machine & Sophie and the Giants",
                        ),
                        cat_song("3", "Higher Love", "Steve Winwood"),
                    ]
                }
            }
        },
    )
    out = await call("search", {"query": "Dopamine", "artist": "EYLR"})
    assert [s["title"] for s in out["songs"]] == ["Dopamine (feat. EYLR)"]
    song = out["songs"][0]
    assert song["featured"] == ["EYLR"] and song["catalog_id"] == "1"
    assert (song["album"], song["isrc"], song["duration_seconds"]) == (
        "Exotica",
        "AUDCB1701788",
        200,
    )
    params = rails.sent(SEARCH)[0].url.params
    assert params["term"] == "Dopamine EYLR" and params["limit"] == "25"
    assert "filtered by artist 'EYLR'" in out["summary"]

    # Whole words only: "Eve" is not a credit on "Steve Winwood".
    none = await call("search", {"artist": "Eve"})
    assert none["songs"] == [] and none["candidates"] == 3
    assert "Apple returned 3" in none["summary"] and "None matched" in none["summary"]

    with pytest.raises(
        ToolError
    ):  # limit is bounded in the schema, not silently clamped
        await call("search", {"query": "x", "limit": 50})


async def test_album_filter(rails):
    rails.on(
        "GET",
        SEARCH,
        200,
        {
            "results": {
                "songs": {
                    "data": [
                        cat_song("1", "Dreams", "Fleetwood Mac", "Rumours"),
                        cat_song(
                            "2",
                            "Dreams (2004 Remaster)",
                            "Fleetwood Mac",
                            "Rumours (Super Deluxe)",
                        ),
                        cat_song("3", "Sara", "Fleetwood Mac", "Tusk"),
                    ]
                }
            }
        },
    )
    out = await call("search", {"query": "Fleetwood Mac", "album": "Rumours"})
    assert [s["id"] for s in out["songs"]] == ["1", "2"]


async def test_library_scope(rails):
    rails.on(
        "GET",
        "/v1/me/library/search",
        200,
        {
            "results": {
                "library-songs": {"data": [lib_song("i.1", "617154366", "Get Lucky")]}
            }
        },
    )
    out = await call("search", {"query": "Get Lucky", "scope": "library"})
    assert out["scope"] == "library"
    assert (out["songs"][0]["id"], out["songs"][0]["catalog_id"]) == (
        "i.1",
        "617154366",
    )
    assert rails.sent("/v1/me/library/search")[0].url.params["types"] == "library-songs"


async def test_search_needs_something_to_look_for(rails):
    with pytest.raises(ToolError, match="query, an artist or an album"):
        await call("search", {})


async def test_lookup_isrc_lists_unmatched_and_invalid(rails):
    rails.on(
        "GET",
        "/v1/catalog/nl/songs",
        200,
        {
            "data": [
                cat_song("1", "Dreams", "Fleetwood Mac", isrc="USWB10400052"),
                cat_song("2", "Get Lucky", "Daft Punk", isrc="USQX91300108"),
            ]
        },
    )
    out = await call(
        "lookup_isrc",
        {"isrcs": ["usqx91300108", "USWB1-0400052", "GBAYE0000001", "abc"]},
    )
    assert [s["id"] for s in out["songs"]] == ["2", "1"]  # input order
    assert out["unmatched"] == ["GBAYE0000001"] and out["invalid"] == ["abc"]
    sent = rails.sent("/v1/catalog/nl/songs")[0].url.params["filter[isrc]"]
    assert sent == "USQX91300108,USWB10400052,GBAYE0000001"


# -- reading playlists --------------------------------------------------------


async def test_list_playlists_shows_editability_and_counts(rails):
    rails.on(
        "GET",
        LISTS,
        200,
        {
            "data": [
                playlist("p.mine", "Mine", count=12),
                playlist("p.musicapp", "Road Trip", count=40, global_id="pl.u-1"),
                playlist(
                    "p.apple",
                    "Today's Hits",
                    can_edit=False,
                    count=50,
                    global_id="pl.f4d1",
                ),
            ]
        },
    )
    srv.store().own("p.mine", "created")
    out = await call("list_playlists")
    rows = {
        p["id"]: (p["editable"], p["apple_curated"], p["track_count"])
        for p in out["playlists"]
    }
    assert rows == {
        "p.mine": (True, False, 12),
        "p.musicapp": (False, False, 40),  # Apple calls it editable; this server won't
        "p.apple": (False, True, 50),
    }
    assert rails.sent(LISTS)[0].url.params["extend"] == "trackCount"
    only = await call("list_playlists", {"editable_only": True})
    assert [p["id"] for p in only["playlists"]] == ["p.mine"]
    named = await call("list_playlists", {"name_contains": "road"})
    assert [p["id"] for p in named["playlists"]] == ["p.musicapp"]


async def test_list_playlists_without_web_rail(rails):
    rails.routes[("GET", "/us/new")] = [httpx.Response(503)]
    rails.on("GET", LISTS, 200, {"data": [playlist("p.mine", "Mine")]})
    out = await call("list_playlists")
    assert out["playlists"][0]["track_count"] is None


async def test_get_playlist_positions_and_paging(rails):
    rails.on("GET", MINE, 200, {"data": [playlist("p.mine", "Mine")]})
    rails.on(
        "GET",
        TRACKS,
        200,
        {"data": [lib_song(f"i.{n}", str(n), f"T{n}") for n in range(1, 6)]},
    )
    out = await call("get_playlist", {"playlist_id": "p.mine", "offset": 2, "limit": 2})
    assert [(t["position"], t["title"]) for t in out["tracks"]] == [
        (3, "T3"),
        (4, "T4"),
    ]
    assert (out["total"], out["truncated"], out["playlist"]["track_count"]) == (
        5,
        True,
        5,
    )
    assert "positions 3-4 of 5 tracks" in out["summary"]
    assert "Not editable by this server" in out["summary"]


# -- editing ------------------------------------------------------------------


async def test_create_with_songs_then_rename(rails):
    rails.on("POST", LISTS, 201, {"data": [{"id": "p.new"}]})
    rails.on("GET", f"{LISTS}/p.new/tracks", 404, {"errors": []})
    rails.on(
        "GET",
        f"{LISTS}/p.new/tracks",
        200,
        {"data": [lib_song("i.1", "100", "Dreams", "Fleetwood Mac")]},
    )
    rails.on("POST", f"{LISTS}/p.new/tracks", 204)
    created = await call("create_playlist", {"name": "ROCK 24", "song_ids": ["100"]})
    assert (created["playlist_id"], created["rail"], created["confirmation"]) == (
        "p.new",
        "api",
        "read_back",
    )
    assert created["added"] == [
        {
            "position": 1,
            "id": "i.1",
            "catalog_id": "100",
            "title": "Dreams",
            "artist": "Fleetwood Mac",
        }
    ]
    assert srv.store().owns("p.new")

    rails.on("GET", f"{LISTS}/p.new", 200, {"data": [playlist("p.new", "ROCK 24")]})
    rails.on("PATCH", f"{LISTS}/p.new", 204)
    renamed = await call("rename_playlist", {"playlist_id": "p.new", "name": "ROCK"})
    assert (renamed["rail"], renamed["previous_name"], renamed["name"]) == (
        "web",
        "ROCK 24",
        "ROCK",
    )


async def test_create_alone_is_apple_accepted(rails):
    rails.on("POST", LISTS, 201, {"data": [{"id": "p.new"}]})
    out = await call("create_playlist", {"name": "Empty"})
    assert (out["confirmation"], out["track_count_after"]) == ("apple_accepted", 0)


async def test_create_keeps_the_playlist_when_adding_fails(rails):
    rails.on("POST", LISTS, 201, {"data": [{"id": "p.new"}]})
    with pytest.raises(
        ToolError, match=r"Created 'X' \(p\.new\), but adding songs failed"
    ):
        await call("create_playlist", {"name": "X", "song_ids": ["i.lib"]})
    assert srv.store().owns("p.new")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("rename_playlist", {"name": "x"}),
        ("set_playlist_description", {"description": "x"}),
        ("delete_playlist", {}),  # refused before even asking for confirm
        ("add_tracks", {"song_ids": ["1"]}),
        ("remove_tracks", {"positions": [1]}),
    ],
)
async def test_not_editable_refused_before_any_request(rails, tool, args):
    with pytest.raises(
        ToolError, match="only edits playlists it created.*create_playlist"
    ):
        await call(tool, {"playlist_id": "p.musicapp", **args})
    assert rails.requests == []


async def test_delete_needs_confirm_and_reports_the_name(rails):
    srv.store().own("p.mine", "created")
    with pytest.raises(ToolError, match="Ask the user"):
        await call("delete_playlist", {"playlist_id": "p.mine"})
    assert writes(rails) == []
    rails.on("GET", MINE, 200, {"data": [playlist("p.mine", "Old Mix")]})
    rails.on("DELETE", MINE, 204)
    out = await call("delete_playlist", {"playlist_id": "p.mine", "confirm": True})
    assert (out["rail"], out["name"]) == ("web", "Old Mix")
    assert not srv.store().owns("p.mine")


async def test_add_by_ids_and_isrcs(rails):
    srv.store().own("p.mine", "created")
    rails.on(
        "GET",
        "/v1/catalog/nl/songs",
        200,
        {
            "data": [
                cat_song("200", "B", "X", isrc="USQX91300108"),
                cat_song("201", "B2", "X", isrc="USQX91300108"),
            ]
        },
    )
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.0", "50")]})
    rails.on(
        "GET",
        TRACKS,
        200,
        {
            "data": [
                lib_song("i.0", "50"),
                lib_song("i.1", "100", "A"),
                lib_song("i.2", "200", "B"),
            ]
        },
    )
    rails.on("POST", TRACKS, 204)
    out = await call(
        "add_tracks",
        {
            "playlist_id": "p.mine",
            "song_ids": ["100"],
            "isrcs": ["USQX91300108", "usqx91300108", "GBAYE0000001", "abc"],
        },
    )
    assert (out["confirmation"], out["rail"], out["track_count_after"]) == (
        "read_back",
        "api",
        3,
    )
    assert [(a["position"], a["catalog_id"]) for a in out["added"]] == [
        (2, "100"),
        (3, "200"),
    ]
    assert (out["unmatched_isrcs"], out["invalid_isrcs"]) == (["GBAYE0000001"], ["abc"])
    posted = json.loads(writes(rails)[0].content)["data"]
    assert [d["id"] for d in posted] == [
        "100",
        "200",
    ]  # the ISRC given twice is added once


async def test_add_keeps_explicit_repeats(rails):
    srv.store().own("p.mine", "created")
    rails.on("GET", TRACKS, 404, {"errors": []})
    rails.on(
        "GET", TRACKS, 200, {"data": [lib_song("i.1", "100"), lib_song("i.1", "100")]}
    )
    rails.on("POST", TRACKS, 204)
    out = await call(
        "add_tracks", {"playlist_id": "p.mine", "song_ids": ["100", "100"]}
    )
    assert [a["position"] for a in out["added"]] == [1, 2]
    assert [d["id"] for d in json.loads(writes(rails)[0].content)["data"]] == [
        "100",
        "100",
    ]


async def test_add_rejects_library_ids_and_big_batches(rails):
    srv.store().own("p.mine", "created")
    with pytest.raises(ToolError, match="library IDs; add_tracks takes catalog_id"):
        await call("add_tracks", {"playlist_id": "p.mine", "song_ids": ["i.ABC"]})
    with pytest.raises(ToolError):
        await call(
            "add_tracks",
            {"playlist_id": "p.mine", "song_ids": [str(n) for n in range(26)]},
        )
    assert rails.requests == []


async def test_add_not_shown_after_write_says_what_to_do(rails):
    srv.store().own("p.mine", "created")
    rails.on("GET", TRACKS, 404, {"errors": []})
    rails.on("POST", TRACKS, 204)
    with pytest.raises(
        ToolError,
        match="doesn't show 1 of 1: 100.*check with get_playlist before retrying",
    ):
        await call("add_tracks", {"playlist_id": "p.mine", "song_ids": ["100"]})


async def test_remove_by_position(rails):
    srv.store().own("p.mine", "created")
    rails.on(
        "GET",
        TRACKS,
        200,
        {"data": [lib_song("i.A", "1", "A"), lib_song("i.B", "2", "B")]},
    )
    rails.on(
        "GET",
        TRACKS,
        200,
        {"data": [lib_song("i.A", "1", "A"), lib_song("i.B", "2", "B")]},
    )
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.A", "1", "A")]})
    rails.on("DELETE", TRACKS, 204)
    out = await call("remove_tracks", {"playlist_id": "p.mine", "positions": [2]})
    assert (out["confirmation"], out["rail"], out["track_count_after"]) == (
        "read_back",
        "web",
        1,
    )
    assert [(r["position"], r["title"]) for r in out["removed"]] == [(2, "B")]
    delete = writes(rails)[0]
    assert (delete.url.params["ids[library-songs]"], delete.url.params["mode"]) == (
        "i.B",
        "first",
    )


async def test_remove_refuses_a_lower_copy_of_a_repeat(rails):
    """[A, B, A]: position 3 would make Apple take position 1 instead."""
    srv.store().own("p.mine", "created")
    rails.on(
        "GET",
        TRACKS,
        200,
        {
            "data": [
                lib_song("i.A", "1", "A"),
                lib_song("i.B", "2", "B"),
                lib_song("i.A", "1", "A"),
            ]
        },
    )
    with pytest.raises(
        ToolError, match=r"'A' is at positions \[1, 3\].*Ask for positions \[1\]"
    ):
        await call("remove_tracks", {"playlist_id": "p.mine", "positions": [3]})
    assert writes(rails) == []


async def test_remove_bad_requests(rails):
    srv.store().own("p.mine", "created")
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.A", "1")]})
    with pytest.raises(ToolError, match=r"No track at positions \[5\]"):
        await call("remove_tracks", {"playlist_id": "p.mine", "positions": [5]})
    with pytest.raises(ToolError, match="not both"):
        await call(
            "remove_tracks",
            {"playlist_id": "p.mine", "positions": [1], "track_ids": ["i.A"]},
        )
    with pytest.raises(ToolError, match=r"fewer copies than asked\): i\.A"):
        await call(
            "remove_tracks", {"playlist_id": "p.mine", "track_ids": ["i.A", "i.A"]}
        )
    assert writes(rails) == []


async def test_remove_stopped_partway(rails):
    srv.store().own("p.mine", "created")
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.A", "1"), lib_song("i.B", "2")]})
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.A", "1"), lib_song("i.B", "2")]})
    rails.on("GET", TRACKS, 200, {"data": [lib_song("i.B", "2")]})
    rails.on("DELETE", TRACKS, 204)
    rails.on("DELETE", TRACKS, 429, {})
    with pytest.raises(
        ToolError, match="stopped partway.*1 of 2 went.*call get_playlist"
    ):
        await call(
            "remove_tracks", {"playlist_id": "p.mine", "track_ids": ["i.A", "i.B"]}
        )


# -- failures an agent can act on ----------------------------------------------


async def test_invalid_user_token_names_the_sign_in_step(rails):
    srv.store().mark_token_invalid(APPLE_USER_TOKEN)
    with pytest.raises(ToolError, match="call sign_in_link, give them the URL"):
        await call("search", {"query": "x", "scope": "library"})
    assert rails.requests == []


async def test_rejected_user_token_is_marked_and_explained(rails):
    rails.on("GET", LISTS, 403, {"errors": [{"title": "Forbidden"}]})
    rails.routes[("GET", "/us/new")] = [
        httpx.Response(503)
    ]  # straight to the documented rail
    with pytest.raises(ToolError, match="sign-in needed"):
        await call("list_playlists")
    assert srv.store().valid_token(APPLE_USER_TOKEN) is None


async def test_rate_limit_is_not_reported_as_absence(rails):
    rails.on("GET", SEARCH, 429, {})
    with pytest.raises(ToolError, match="rate limiting") as e:
        await call("search", {"query": "x"})
    assert "not found'" in str(e.value)  # says it does NOT mean not found


async def test_sign_in_link_works_once(rails):
    out = await call("sign_in_link")
    assert out["expires_in_seconds"] == 900
    path = httpx.URL(out["url"])
    transport = httpx.ASGITransport(app=srv.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        first = await http.get(path.path, params=dict(path.params))
        second = await http.get(path.path, params=dict(path.params))
    assert (first.status_code, second.status_code) == (200, 403)


async def test_set_description_is_read_back(rails):
    srv.store().own("p.mine", "test")
    old = {"standard": "24/7 Rock\n\t•\tskip at least the first 2-3 tracks."}
    new = "24/7 Rock\n\t•\tskip at least the first 3-4 tracks."
    rails.on(
        "GET",
        f"{LISTS}/p.mine",
        200,
        {
            "data": [
                {
                    **playlist("p.mine", "ROCK"),
                    "attributes": {"name": "ROCK", "description": old},
                }
            ]
        },
    )
    rails.on(
        "GET",
        f"{LISTS}/p.mine",
        200,
        {
            "data": [
                {
                    **playlist("p.mine", "ROCK"),
                    "attributes": {"name": "ROCK", "description": {"standard": new}},
                }
            ]
        },
    )
    rails.on("PATCH", f"{LISTS}/p.mine", 204)
    out = await call(
        "set_playlist_description", {"playlist_id": "p.mine", "description": new}
    )
    assert (out["confirmation"], out["description"], out["previous_description"]) == (
        "read_back",
        new,
        old["standard"],
    )
    sent = [r for r in rails.requests if r.method == "PATCH"]
    assert json.loads(sent[0].content) == {"attributes": {"description": new}}
