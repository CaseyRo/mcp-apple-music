"""MCP server for Apple Music."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import canary
from . import models as m
from . import signin
from .apple_api import AppleMusicAPI
from .apple_web import AppleWebPlayer, playlist_kind
from .auth import BearerTokenVerifier
from .config import settings
from .devtoken import DeveloperToken
from .library import Library
from .matching import Matcher
from .mirror import Engine, coverage, event
from .mirror_config import ConfigError, Mirror
from .mirror_config import load as load_mirrors
from .mirror_config import loads as load_mirrors_inline
from .rails import (
    AppleError,
    RailUnavailable,
    RateLimited,
    UserTokenMissing,
    UserTokenRejected,
)
from .scheduler import Scheduler
from .sources import Feed, track_from_payload
from .spotify import SpotifyClient
from .store import APPLE_USER_TOKEN, LINK_TTL_SECONDS, Store
from .usage import UsageMiddleware

try:
    # Releases are git tags; the image carries the tag as APP_VERSION.
    __version__ = os.environ.get("APP_VERSION") or version("mcp-apple-music")
except PackageNotFoundError:  # running from a source tree
    __version__ = "dev"

_start_time = datetime.now(timezone.utc)

_auth = None
if settings.mcp_api_key.get_secret_value():
    _auth = BearerTokenVerifier(settings.mcp_api_key.get_secret_value())

INSTRUCTIONS = """\
Apple Music: search the catalog and the owner's library, and manage playlists.
- Editing: this server changes only playlists it created or a mirror adopted
  (`editable` in list_playlists). For any other playlist, create a new one with
  create_playlist, which can take the songs in the same call.
- Two kinds of ID: add_tracks takes catalog IDs (`catalog_id` from search,
  lookup_isrc or get_playlist); remove_tracks takes positions or track `id`s from
  get_playlist.
- Copies of a song in a playlist share an ID, and Apple removes the topmost copy.
- Adds and removals are confirmed by re-reading the playlist, but are not
  idempotent: after an error, call get_playlist before retrying.
- If a tool says sign-in is needed, call sign_in_link and give the URL to the
  user to open in their browser; never open it yourself. Retry after they confirm.
- Mirrors keep a playlist equal to a source's last hours of music (mirror_status,
  mirror_audit, sync_mirror). Don't edit a mirror's playlist by hand: the next
  sync undoes it.
"""


CANARY = "canary"


async def run_canary() -> None:
    """One canary run, recorded for /health. Skipped (not failed) while nobody is
    signed in, since a missing sign-in is reported on its own."""
    if not developer_token() or not store().valid_token(APPLE_USER_TOKEN):
        return
    result = await canary.run(library())
    store().set_check(CANARY, result.ok, result.error or "; ".join(result.steps))
    event("canary", "", ok=result.ok, error=result.error)


async def _canary_once() -> None:
    try:
        await run_canary()
    except Exception as e:  # noqa: BLE001 - a canary bug must not stop the server
        event("canary_error", "", error=repr(e))


async def _canary_loop() -> None:
    while True:
        await _canary_once()
        await asyncio.sleep(settings.canary_interval_seconds)


_after_sign_in: set[asyncio.Task] = set()  # keeps the task alive until it ends


@lifespan
async def _background(server):
    """The mirror loop (with MIRRORS_ENABLED) and, when serving HTTP, the canary."""
    tasks = []
    if settings.mirrors_enabled:
        try:
            tasks.append(asyncio.create_task(scheduler().run()))
        except (ConfigError, ToolError) as e:  # health shows why
            event("loop_not_started", "", error=str(e))
    if settings.transport == "http" and settings.canary_interval_seconds > 0:
        tasks.append(asyncio.create_task(_canary_loop()))
    try:
        yield {}
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


mcp = FastMCP(
    "mcp-apple-music", instructions=INSTRUCTIONS, auth=_auth, lifespan=_background
)
mcp.add_middleware(UsageMiddleware("apple-music"))

MAX_BODY_BYTES = 64 * 1024


# -- state, built on first use ------------------------------------------------


@functools.cache
def store() -> Store:
    return Store(settings.data_dir / "state.db")


@functools.cache
def developer_token() -> DeveloperToken | None:
    key = settings.apple_private_key.get_secret_value()
    if not (settings.apple_team_id and settings.apple_key_id and key):
        return None
    return DeveloperToken(
        settings.apple_team_id,
        settings.apple_key_id,
        key,
        ttl_seconds=settings.apple_token_ttl_seconds,
        renew_before_seconds=settings.apple_token_renew_before_seconds,
    )


@functools.cache
def apple_api() -> AppleMusicAPI:
    dev = developer_token()
    if dev is None:
        raise ToolError(
            "MusicKit key not configured: set APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_PRIVATE_KEY."
        )
    return AppleMusicAPI(
        dev,
        user_token=lambda: store().valid_token(APPLE_USER_TOKEN),
        on_user_token_rejected=lambda: store().mark_token_invalid(APPLE_USER_TOKEN),
    )


@functools.cache
def apple_web() -> AppleWebPlayer:
    return AppleWebPlayer(
        user_token=lambda: store().valid_token(APPLE_USER_TOKEN),
        on_user_token_rejected=lambda: store().mark_token_invalid(APPLE_USER_TOKEN),
    )


@functools.cache
def library() -> Library:
    return Library(apple_api(), apple_web())


@functools.cache
def mirrors() -> list[Mirror]:
    """The configured mirrors. Raises ConfigError for an invalid file (not cached,
    so a fixed file is picked up on the next call)."""
    if settings.mirrors_file:
        return load_mirrors(settings.mirrors_file)
    return (
        load_mirrors_inline(settings.mirrors_config) if settings.mirrors_config else []
    )


@functools.cache
def spotify() -> SpotifyClient | None:
    secret = settings.spotify_client_secret.get_secret_value()
    if not (settings.spotify_client_id and secret):
        return None
    return SpotifyClient(settings.spotify_client_id, secret)


@functools.cache
def engine() -> Engine:
    return Engine(store(), library(), Matcher(apple_api(), store(), spotify()))


@functools.cache
def scheduler() -> Scheduler:
    return Scheduler(engine(), Feed(store()), mirrors())


def public_url() -> str:
    return settings.public_url or f"http://127.0.0.1:{settings.port}"


async def read_limited(request: Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """The request body, or None once it passes ``limit`` (callers answer 413)."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


# -- health -------------------------------------------------------------------


def _ts(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _credentials() -> dict:
    """Each credential's state; never a value."""
    now = datetime.now(timezone.utc).timestamp()
    try:
        dev = developer_token()
        developer = {"state": "valid" if dev else "missing"}
    except Exception as e:  # noqa: BLE001 - a malformed key raises on first mint
        developer = {"state": "invalid", "error": type(e).__name__}
    web = apple_web().token_state()
    if web.get("expires_at"):
        web["expires_at"] = _ts(web["expires_at"])
    tok = store().token(APPLE_USER_TOKEN)
    if tok is None:
        user = {"state": "missing"}
    else:
        age = (now - tok.obtained_at) / 86_400
        state = "invalid" if tok.invalid else "valid"
        if state == "valid" and age >= settings.apple_user_token_warn_days:
            state = "expiring soon"
        user = {
            "state": state,
            "obtained_at": _ts(tok.obtained_at),
            "age_days": int(age),
        }
    sp = bool(
        settings.spotify_client_id and settings.spotify_client_secret.get_secret_value()
    )
    return {
        "developer_token": developer,
        "web_player_token": web,
        "apple_user_token": user,
        "spotify_app": {"state": "configured" if sp else "missing"},
        "spotify_owner_login": {"state": "not used"},  # until a Spotify source exists
    }


def _canary_health() -> dict:
    row = store().check(CANARY)
    if row is None:
        return {"state": "not run yet"}
    return {
        "state": "ok" if row["ok"] else "failed",
        "at": _ts(row["at"]),
        **({} if row["ok"] else {"error": row["detail"]}),
    }


def _problems(creds: dict, mirrors_block: dict, canary_block: dict) -> list[str]:
    problems = []
    if creds["developer_token"]["state"] != "valid":
        problems.append(f"MusicKit key {creds['developer_token']['state']}")
    user = creds["apple_user_token"]["state"]
    if user == "missing":
        problems.append("Apple Music sign-in needed")
    elif user != "valid":
        problems.append(f"Apple Music sign-in {user}: sign in again")
    if creds["web_player_token"]["state"] == "invalid":
        problems.append("web-player token unavailable: removals are paused")
    if canary_block["state"] == "failed":
        problems.append("canary failed")
    if mirrors_block.get("config_error"):
        problems.append("mirrors file invalid")
    problems += [f"mirror {name} failing" for name in mirrors_block.get("failing", [])]
    return problems


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Public health endpoint: states and times, never a secret."""
    creds, mirrors_block, canary_block = (
        _credentials(),
        _mirrors_health(),
        _canary_health(),
    )
    problems = _problems(creds, mirrors_block, canary_block)
    return JSONResponse(
        {
            "status": "degraded" if problems else "healthy",
            "problems": problems,
            "service": "mcp-apple-music",
            "version": __version__,
            "uptime_seconds": int(
                (datetime.now(timezone.utc) - _start_time).total_seconds()
            ),
            "credentials": creds,
            "canary": canary_block,
            "mirrors": mirrors_block,
        }
    )


def _failing(row) -> bool:
    return bool(row and row["last_error"]) and (row["last_error_at"] or 0) > (
        row["last_sync"] or 0
    )


def _mirrors_health() -> dict:
    try:
        configured = mirrors()
    except ConfigError as e:
        return {"enabled": settings.mirrors_enabled, "config_error": str(e)}
    now = datetime.now(timezone.utc).timestamp()
    rows = {m.name: store().mirror(m.name) for m in configured}
    return {
        "enabled": settings.mirrors_enabled,
        "count": len(configured),
        "failing": [name for name, row in rows.items() if _failing(row)],
        "each": [
            {
                "name": m.name,
                "last_sync": _ts(rows[m.name]["last_sync"]) if rows[m.name] else None,
                "coverage": (
                    round(cov, 3)
                    if (cov := coverage(store(), m, now)[0]) is not None
                    else None
                ),
            }
            for m in configured
        ],
    }


# -- Apple sign-in (gated by a single-use link, not the bearer key) ---------------


@mcp.custom_route("/auth/apple", methods=["GET"])
async def apple_sign_in_page(request: Request) -> HTMLResponse | PlainTextResponse:
    dev = developer_token()
    if dev is None:  # checked first, so a misconfigured server doesn't burn the link
        return PlainTextResponse(
            "MusicKit key not configured (APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY).",
            503,
        )
    link = request.query_params.get("t", "")
    if not store().open_link("apple", link):
        return HTMLResponse(signin.refused_page(), 403, headers=signin.HEADERS)
    return HTMLResponse(
        signin.apple_page(dev.get(), link, __version__), headers=signin.HEADERS
    )


@mcp.custom_route("/auth/apple", methods=["POST"])
async def apple_sign_in_complete(request: Request) -> JSONResponse:
    def answer(status: int, message: str) -> JSONResponse:
        return JSONResponse(
            {"ok": status == 200, "message": message}, status, headers=signin.HEADERS
        )

    raw = await read_limited(request)
    if raw is None:
        return answer(413, "Request too large.")
    try:
        body = json.loads(raw)
        link, mut = body["t"], body["music_user_token"]
    except (ValueError, KeyError, TypeError):
        return answer(400, "Malformed sign-in request.")
    if not (isinstance(link, str) and isinstance(mut, str) and 0 < len(mut) <= 4096):
        return answer(400, "Malformed sign-in request.")
    if not store().complete_link("apple", link):
        return answer(403, signin.REFUSED)

    store().set_token(APPLE_USER_TOKEN, mut)
    api = apple_api()
    api.forget_storefront()
    try:
        storefront = await api.storefront()
    except UserTokenRejected:
        return answer(
            400, "Apple rejected this sign-in. Open a new link and try again."
        )
    except AppleError as e:
        return answer(502, f"Signed in, but Apple could not confirm it yet: {e}")
    if (
        settings.canary_interval_seconds > 0
    ):  # check both rails now, not at the next run
        task = asyncio.create_task(_canary_once())
        _after_sign_in.add(task)
        task.add_done_callback(_after_sign_in.discard)
    return answer(200, f"Signed in (storefront {storefront}). You can close this page.")


# -- paths outside bearer auth ---------------------------------------------------


class GateUnknownPaths:
    """Without the API key, only /health, /hooks/* and /auth/* answer (each has its
    own gate); /mcp has FastMCP's bearer check. Anything else is a 401, so the
    server's surface can't be mapped without the key."""

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self.expected = f"Bearer {api_key}".encode()

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        gated_elsewhere = path in ("/mcp", "/health") or path.startswith(
            ("/mcp/", "/hooks/", "/auth/")
        )
        given = dict(scope.get("headers", [])).get(b"authorization", b"")
        if (
            scope["type"] == "http"
            and not gated_elsewhere
            and not hmac.compare_digest(given, self.expected)
        ):
            await JSONResponse({"error": "Unauthorized"}, 401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def http_middleware() -> list:
    key = settings.mcp_api_key.get_secret_value()
    return [StarletteMiddleware(GateUnknownPaths, api_key=key)] if key else []


# -- webhook: tracks pushed by a station (gated by the path secret) ---------------

WEBHOOK_MAX_BYTES = 16 * 1024


@mcp.custom_route("/hooks/{secret}", methods=["POST"])
async def webhook(request: Request) -> JSONResponse:
    """Stores the track and answers at once; the mirror loop matches and writes it."""
    expected = settings.webhook_secret.get_secret_value().encode()
    given = request.path_params.get("secret", "").encode()
    if not expected or not hmac.compare_digest(given, expected):
        return JSONResponse({"error": "Not Found"}, 404)
    raw = await read_limited(request, WEBHOOK_MAX_BYTES)
    if raw is None:
        return JSONResponse({"error": "Request too large."}, 413)
    try:
        body = json.loads(raw)
        track = track_from_payload(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    key = str(body.get("playlist") or "").casefold()
    try:
        mirror = next((m for m in mirrors() if m.key == key), None)
        ingest = engine().ingest
    except (ConfigError, ToolError) as e:
        return JSONResponse({"error": f"Mirrors unavailable: {e}"}, 503)
    if mirror is None:
        return JSONResponse({"error": f"No mirror is bound to playlist {key!r}."}, 422)
    new = ingest(mirror, [track], "webhook")
    if new and settings.mirrors_enabled:
        scheduler().wake(mirror.name)
    return JSONResponse({"accepted": True, "new": bool(new)}, 202)


# -- tools ----------------------------------------------------------------------

_READ = dict(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
_TAGS = {"apple-music"}
MAX_BATCH = (
    25  # songs per add/remove call: keeps a call well inside the portal's time budget
)
SIGN_IN_HINT = (
    "Ask the user to sign in: call sign_in_link, give them the URL to open in their "
    "browser (don't open it yourself; it works once), then retry once they confirm."
)
NOT_EDITABLE = (
    "This server only edits playlists it created or a mirror adopted. For an "
    "editable copy, call "
    "create_playlist with the songs' catalog_id values (from get_playlist)."
)
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
SearchType = Literal["songs", "albums", "artists", "playlists"]
Batch = Annotated[list[str], Field(max_length=MAX_BATCH)]


@contextlib.contextmanager
def _apple_errors():
    """Rail errors become tool errors that say what to do next."""
    try:
        yield
    except (UserTokenMissing, UserTokenRejected) as e:
        raise ToolError(
            f"Apple Music sign-in needed ({e.reason}). {SIGN_IN_HINT}"
        ) from e
    except RateLimited as e:
        raise ToolError(
            f"Apple is rate limiting the {e.rail} rail, for up to an hour. Retry later; "
            "this does not mean 'not found'."
        ) from e
    except RailUnavailable as e:
        raise ToolError(
            f"{e.reason}. Removing tracks, renaming and deleting need the web-player "
            "rail; search, create and add still work."
        ) from e
    except AppleError as e:
        raise ToolError(str(e)) from e


def _owned(playlist_id: str) -> None:
    if not store().owns(playlist_id):
        raise ToolError(f"Playlist {playlist_id} is not editable here. {NOT_EDITABLE}")


class SearchResult(BaseModel):
    summary: str
    scope: Literal["catalog", "library"]
    songs: list[m.Song] = []
    albums: list[m.Album] = []
    artists: list[m.Artist] = []
    playlists: list[m.PlaylistRef] = []
    candidates: int = Field(
        description="Results Apple returned before the artist/album filters."
    )
    possibly_throttled: bool = False


class IsrcResult(BaseModel):
    summary: str
    songs: list[m.Song] = Field(
        description="Every match, in the order the ISRCs were given."
    )
    unmatched: list[str] = Field(
        description="Valid ISRCs with no song in this storefront."
    )
    invalid: list[str] = Field(description="Inputs that are not ISRCs.")
    possibly_throttled: bool = False


class PlaylistInfo(BaseModel):
    id: str
    name: str
    editable: bool = Field(
        description="True only if this server created it or a mirror adopted it; "
        "only those can be changed here."
    )
    apple_curated: bool = Field(
        description="An Apple-made playlist added to the library."
    )
    track_count: int | None = None
    description: str | None = None


class PlaylistsResult(BaseModel):
    summary: str
    playlists: list[PlaylistInfo]


class PlaylistTrack(m.Song):
    position: int = Field(description="1-based position, as remove_tracks takes it.")


class PlaylistTracks(BaseModel):
    summary: str
    playlist: PlaylistInfo
    tracks: list[PlaylistTrack]
    total: int
    truncated: bool = Field(
        description="More tracks follow; call again with a larger offset."
    )


class TrackChange(BaseModel):
    position: int = Field(description="Where it sat (removed) or now sits (added).")
    id: str
    catalog_id: str | None
    title: str
    artist: str


class PlaylistChange(BaseModel):
    summary: str
    playlist_id: str
    name: str | None = None
    rail: Literal["api", "web"]
    confirmation: Literal["read_back", "apple_accepted"] = Field(
        description="read_back: the playlist was re-read and shows the change. "
        "apple_accepted: Apple accepted it; nothing to re-read (create, rename, delete)."
    )
    added: list[TrackChange] = []
    removed: list[TrackChange] = []
    track_count_after: int | None = None
    previous_name: str | None = None
    description: str | None = None
    previous_description: str | None = None
    unmatched_isrcs: list[str] = []
    invalid_isrcs: list[str] = []


class SignInLink(BaseModel):
    summary: str
    url: str
    expires_in_seconds: int


def _playlist_info(resource: dict) -> PlaylistInfo:
    a = resource.get("attributes", {})
    return PlaylistInfo(
        id=resource["id"],
        name=a.get("name", ""),
        editable=store().owns(resource["id"]),
        apple_curated=playlist_kind(resource) == "apple",
        track_count=a.get("trackCount"),
        description=_description(resource),
    )


def _description(resource: dict) -> str | None:
    return (resource.get("attributes", {}).get("description") or {}).get("standard")


def _change(position: int, song: m.Song) -> TrackChange:
    return TrackChange(
        position=position,
        id=song.id,
        catalog_id=song.catalog_id,
        title=song.title,
        artist=song.artist,
    )


async def _resolve_isrcs(isrcs: list[str]):
    """(songs per valid ISRC in input order, unmatched, invalid, possibly_throttled)."""
    wanted: list[str] = []
    invalid: list[str] = []
    for raw in isrcs:
        code = re.sub(r"[\s-]", "", raw).upper()
        if _ISRC.match(code):
            wanted.append(code)
        else:
            invalid.append(raw)
    wanted = list(dict.fromkeys(wanted))
    by_isrc: dict[str, list[m.Song]] = {}
    throttled = False
    if wanted:
        read = await apple_api().songs_by_isrc(wanted)
        throttled = read.possibly_throttled
        for r in read.data:
            s = m.song(r)
            by_isrc.setdefault((s.isrc or "").upper(), []).append(s)
    found = {code: by_isrc[code] for code in wanted if code in by_isrc}
    unmatched = [code for code in wanted if code not in by_isrc]
    return found, unmatched, invalid, throttled


async def _add(
    playlist_id: str, song_ids: list[str], isrcs: list[str]
) -> PlaylistChange:
    if len(song_ids) + len(isrcs) > MAX_BATCH:
        raise ToolError(
            f"Add at most {MAX_BATCH} songs per call; call again for the rest."
        )
    library_ids = [i for i in song_ids if i.startswith(("i.", "l."))]
    if library_ids:
        raise ToolError(
            f"{', '.join(library_ids)} are library IDs; add_tracks takes catalog_id "
            "values (from search, lookup_isrc or get_playlist)."
        )
    ids = list(song_ids)  # a song listed twice is added twice: repeats are allowed
    unmatched: list[str] = []
    invalid: list[str] = []
    with _apple_errors():
        if isrcs:
            found, unmatched, invalid, _ = await _resolve_isrcs(isrcs)
            # Several releases per ISRC: the first one, as the mirrors pick.
            ids += [
                s[0].catalog_id for s in found.values() if s[0].catalog_id not in ids
            ]
        if not ids:
            raise ToolError(
                "Nothing to add: no song IDs, and no ISRC matched."
                + (f" Unmatched: {', '.join(unmatched)}." if unmatched else "")
                + (f" Not ISRCs: {', '.join(invalid)}." if invalid else "")
            )
        result = await library().add_tracks(playlist_id, ids)
    if result.tracks is None:
        raise ToolError(
            f"Apple accepted the add, but reading the playlist back failed ({result.reason}). "
            "The songs probably landed: check with get_playlist before retrying, "
            "since a retry adds duplicates."
        )
    if not result.ok:
        raise ToolError(
            f"Apple accepted the add, but after several seconds the playlist doesn't show "
            f"{len(result.failed)} of {len(ids)}: {', '.join(result.failed)}. They may still "
            "appear: check with get_playlist before retrying, since a retry can duplicate "
            "them. If they never appear, the ID is not a catalog song in this storefront."
        )
    # Appends land at the end, in order.
    tail = result.tracks[-len(ids) :]
    first_pos = len(result.tracks) - len(ids) + 1
    note = ""
    if unmatched or invalid:
        note = " Skipped" + (
            f" unmatched ISRCs {', '.join(unmatched)}" if unmatched else ""
        )
        note += (
            (";" if unmatched and invalid else "")
            + (f" non-ISRCs {', '.join(invalid)}" if invalid else "")
            + "."
        )
    return PlaylistChange(
        summary=f"Added {len(ids)} songs at the end of {playlist_id} via the {result.rail} "
        f"rail; re-read and confirmed.{note}",
        playlist_id=playlist_id,
        rail=result.rail,
        confirmation="read_back",
        added=[_change(first_pos + i, m.song(t)) for i, t in enumerate(tail)],
        track_count_after=len(result.tracks),
        unmatched_isrcs=unmatched,
        invalid_isrcs=invalid,
    )


@mcp.tool(tags=_TAGS, annotations=ToolAnnotations(title="Search Apple Music", **_READ))
async def search(
    query: str = "",
    scope: Literal["catalog", "library"] = "catalog",
    types: list[SearchType] | None = None,
    artist: str | None = None,
    album: str | None = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> SearchResult:
    """[apple-music] Find songs, albums, artists or playlists in the Apple Music
    catalog, or in my library with scope="library". `types` defaults to songs.

    `artist` keeps songs, albums and artists where any credited artist matches,
    featured ones included ("EYLR" finds "Dopamine (feat. EYLR)"). `album` keeps
    songs and albums whose album title matches. Both match whole words, ignoring
    case and accents, are added to the search terms, and don't apply to playlists.
    A song's `catalog_id` is what add_tracks takes.
    """
    term = " ".join(
        dict.fromkeys(
            t.strip() for t in (query, artist or "", album or "") if t.strip()
        )
    )
    if not term:
        raise ToolError("Give a query, an artist or an album to search for.")
    types = types or ["songs"]
    fetch = 25 if (artist or album) else limit
    with _apple_errors():
        api = apple_api()
        if scope == "library":
            read = await api.search_library(
                term, tuple(f"library-{t}" for t in types), fetch
            )
        else:
            read = await api.search_catalog(term, tuple(types), fetch)

    out = SearchResult(
        summary="",
        scope=scope,
        candidates=len(read.data),
        possibly_throttled=read.possibly_throttled,
    )
    for r in read.data:
        kind = r.get("type", "").removeprefix("library-")
        if kind == "songs":
            s = m.song(r)
            if m.song_matches(s, artist, album):
                out.songs.append(s)
        elif kind == "albums":
            a = m.album(r)
            if (not artist or m.matches(artist, a.artist)) and (
                not album or m.matches(album, a.title)
            ):
                out.albums.append(a)
        elif kind == "artists":
            ar = m.artist(r)
            if not artist or m.matches(artist, ar.name):
                out.artists.append(ar)
        elif kind == "playlists":
            out.playlists.append(m.playlist_ref(r))
    for field in ("songs", "albums", "artists", "playlists"):
        setattr(out, field, getattr(out, field)[:limit])

    counts = ", ".join(f"{len(getattr(out, t))} {t}" for t in types)
    out.summary = f"{counts} in the {scope} for '{term}'."
    filters = " and ".join(
        f"{k} '{v}'" for k, v in (("artist", artist), ("album", album)) if v
    )
    if filters:
        out.summary += f" Apple returned {out.candidates}; filtered by {filters}."
        if out.candidates and not any(getattr(out, t) for t in types):
            out.summary += (
                " None matched: try without the filter, or search the title alone."
            )
    if out.possibly_throttled:
        out.summary += " Apple rate-limited this rail recently, so an empty result may not mean 'none'."
    return out


@mcp.tool(
    tags=_TAGS, annotations=ToolAnnotations(title="Look up songs by ISRC", **_READ)
)
async def lookup_isrc(isrcs: Annotated[list[str], Field(max_length=100)]) -> IsrcResult:
    """[apple-music] Resolve ISRCs (e.g. from Spotify or a label sheet) to Apple
    Music catalog songs in one call. ISRCs with no song in this storefront are
    listed under `unmatched`, never dropped. One ISRC can match several releases;
    all are returned, so pick one and pass its catalog_id to add_tracks."""
    with _apple_errors():
        found, unmatched, invalid, throttled = await _resolve_isrcs(isrcs)
    if not found and not unmatched:
        raise ToolError("No valid ISRC given: 12 characters like USUM71703861.")
    songs = [s for matches in found.values() for s in matches]
    n = len(found) + len(unmatched)
    summary = f"{len(found)} of {n} ISRCs matched ({len(songs)} songs)."
    if unmatched:
        summary += f" Unmatched: {', '.join(unmatched)}."
    if invalid:
        summary += f" Not ISRCs: {', '.join(invalid)}."
    if throttled and unmatched:
        summary += (
            " Apple rate-limited this rail recently, so some misses may be throttling."
        )
    return IsrcResult(
        summary=summary,
        songs=songs,
        unmatched=unmatched,
        invalid=invalid,
        possibly_throttled=throttled,
    )


@mcp.tool(tags=_TAGS, annotations=ToolAnnotations(title="List my playlists", **_READ))
async def list_playlists(
    name_contains: str | None = None, editable_only: bool = False
) -> PlaylistsResult:
    """[apple-music] My library playlists with track counts, and whether this
    server can change each one (`editable`: only playlists it created or a mirror
    adopted). `name_contains` narrows by name, ignoring case."""
    with _apple_errors():
        try:
            resources = (await apple_web().library_playlists()).data
        except (UserTokenMissing, UserTokenRejected):
            raise
        except AppleError:
            # Track counts only come from the web player; the list itself doesn't need it.
            resources = (await apple_api().library_playlists()).data
    playlists = [_playlist_info(r) for r in resources]
    if name_contains:
        needle = name_contains.casefold()
        playlists = [p for p in playlists if needle in p.name.casefold()]
    if editable_only:
        playlists = [p for p in playlists if p.editable]
    n_editable = sum(p.editable for p in playlists)
    return PlaylistsResult(
        summary=f"{len(playlists)} playlists, {n_editable} editable by this server.",
        playlists=playlists,
    )


@mcp.tool(tags=_TAGS, annotations=ToolAnnotations(title="Read a playlist", **_READ))
async def get_playlist(
    playlist_id: str,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> PlaylistTracks:
    """[apple-music] A library playlist's tracks in order, each with its
    `position`, title, artists, album and duration. Track `id`s and positions are
    what remove_tracks takes; `catalog_id`s are what add_tracks takes."""
    with _apple_errors():
        api = apple_api()
        resource = await api.library_playlist(playlist_id)
        everything = (await api.playlist_tracks(playlist_id)).data
    info = _playlist_info(resource)
    info.track_count = len(everything)
    page = everything[offset : offset + limit]
    tracks = [
        PlaylistTrack(position=offset + i + 1, **m.song(t).model_dump())
        for i, t in enumerate(page)
    ]
    minutes = sum(m.song(t).duration_seconds or 0 for t in everything) // 60
    shown = (
        f"positions {offset + 1}-{offset + len(tracks)} of "
        if len(tracks) < len(everything)
        else ""
    )
    return PlaylistTracks(
        summary=f"'{info.name}': {shown}{len(everything)} tracks, {minutes // 60} h {minutes % 60} min."
        + ("" if info.editable else " Not editable by this server."),
        playlist=info,
        tracks=tracks,
        total=len(everything),
        truncated=offset + len(tracks) < len(everything),
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Create a playlist",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def create_playlist(
    name: str,
    description: str = "",
    song_ids: Batch | None = None,
    isrcs: Batch | None = None,
) -> PlaylistChange:
    """[apple-music] Create a playlist in my library, optionally filled in the same
    call with catalog songs (`song_ids`, i.e. catalog_id values, and/or `isrcs`).
    This server can edit it afterwards."""
    if not name.strip():
        raise ToolError("A playlist needs a name.")
    with _apple_errors():
        write = await apple_api().create_playlist(name.strip(), description)
    store().own(write.id, "created")
    if not (song_ids or isrcs):
        return PlaylistChange(
            summary=f"Created '{name.strip()}' ({write.id}), editable by this server.",
            playlist_id=write.id,
            name=name.strip(),
            rail=write.rail,
            confirmation="apple_accepted",
            track_count_after=0,
        )
    try:
        added = await _add(write.id, song_ids or [], isrcs or [])
    except ToolError as e:
        raise ToolError(
            f"Created '{name.strip()}' ({write.id}), but adding songs failed: {e} "
            "The playlist exists and is editable; use add_tracks on it."
        ) from e
    added.name = name.strip()
    added.summary = f"Created '{name.strip()}' ({write.id}). {added.summary}"
    return added


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Rename a playlist",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def rename_playlist(playlist_id: str, name: str) -> PlaylistChange:
    """[apple-music] Rename a playlist this server created or a mirror adopted.
    The previous name is returned."""
    if not name.strip():
        raise ToolError("A playlist needs a name.")
    _owned(playlist_id)
    with _apple_errors():
        previous = (await apple_api().library_playlist(playlist_id))["attributes"].get(
            "name"
        )
        write = await apple_web().rename_playlist(playlist_id, name.strip())
    return PlaylistChange(
        summary=f"Renamed '{previous}' to '{name.strip()}' via the {write.rail} rail.",
        playlist_id=playlist_id,
        name=name.strip(),
        previous_name=previous,
        rail=write.rail,
        confirmation="apple_accepted",
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Set a playlist's description",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def set_playlist_description(
    playlist_id: str, description: Annotated[str, Field(max_length=2000)]
) -> PlaylistChange:
    """[apple-music] Replace the description (the text under the title) of a
    playlist this server created or a mirror adopted. Line breaks and bullets
    are kept. The previous description is returned so it can be restored."""
    _owned(playlist_id)
    with _apple_errors():
        api = apple_api()
        previous = _description(await api.library_playlist(playlist_id))
        write = await apple_web().set_playlist_description(playlist_id, description)
        now = previous
        for delay in (0.0, 1.0, 2.0, 4.0):  # Apple's reads can lag its writes
            await asyncio.sleep(delay)
            now = _description(await api.library_playlist(playlist_id))
            if (now or "").strip() == description.strip():
                break
    confirmed = (now or "").strip() == description.strip()
    return PlaylistChange(
        summary=f"Description of {playlist_id} "
        + (
            "replaced; re-read and confirmed."
            if confirmed
            else "sent; Apple accepted it, but the re-read doesn't show it yet."
        ),
        playlist_id=playlist_id,
        rail=write.rail,
        confirmation="read_back" if confirmed else "apple_accepted",
        description=now,
        previous_description=previous,
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Delete a playlist",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def delete_playlist(playlist_id: str, confirm: bool = False) -> PlaylistChange:
    """[apple-music] Delete a playlist this server created or a mirror adopted.
    It can't be undone: ask the user first, and set `confirm=true` only after
    they agree."""
    _owned(playlist_id)
    if not confirm:
        raise ToolError(
            "Deleting a playlist can't be undone. Ask the user; if they agree, call "
            "again with confirm=true."
        )
    with _apple_errors():
        name = (await apple_api().library_playlist(playlist_id))["attributes"].get(
            "name"
        )
        write = await apple_web().delete_playlist(playlist_id)
    store().disown(playlist_id)
    return PlaylistChange(
        summary=f"Deleted '{name}' ({playlist_id}) via the {write.rail} rail.",
        playlist_id=playlist_id,
        name=name,
        rail=write.rail,
        confirmation="apple_accepted",
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Add songs to a playlist",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def add_tracks(
    playlist_id: str, song_ids: Batch | None = None, isrcs: Batch | None = None
) -> PlaylistChange:
    """[apple-music] Append catalog songs at the end of a playlist this server
    created or a mirror adopted: `song_ids` are catalog_id values (never library
    `i.…` IDs), `isrcs` are resolved to their first release (to choose another,
    call lookup_isrc and pass song_ids). At most 25 per call. Succeeds only when
    a re-read shows the songs; not idempotent, so after an error check
    get_playlist before retrying."""
    _owned(playlist_id)
    return await _add(playlist_id, song_ids or [], isrcs or [])


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Remove songs from a playlist",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def remove_tracks(
    playlist_id: str,
    positions: Annotated[list[int], Field(max_length=MAX_BATCH)] | None = None,
    track_ids: Batch | None = None,
) -> PlaylistChange:
    """[apple-music] Remove songs from a playlist this server created or a mirror
    adopted, by `positions` (from get_playlist) or by track `id`s, not both; each
    removes one copy. Copies of a song share an ID and Apple always removes the
    topmost one, so a position below another copy of the same song is refused.
    At most 25 per call. Not idempotent: after an error, check get_playlist first."""
    _owned(playlist_id)
    if positions and track_ids:
        raise ToolError("Pass positions or track_ids, not both.")
    if not (positions or track_ids):
        raise ToolError("Nothing to remove: give positions or track IDs.")
    with _apple_errors():
        listing = [
            m.song(t) for t in (await apple_api().playlist_tracks(playlist_id)).data
        ]
    at: dict[str, list[int]] = {}  # track ID -> its positions, top first
    for pos, s in enumerate(listing, start=1):
        at.setdefault(s.id, []).append(pos)

    if positions:
        wanted = sorted(set(positions))
        bad = [p for p in wanted if not 1 <= p <= len(listing)]
        if bad:
            raise ToolError(
                f"No track at positions {bad}; the playlist has {len(listing)}."
            )
        ids = [listing[p - 1].id for p in wanted]
        for tid in dict.fromkeys(ids):
            asked = [p for p in wanted if listing[p - 1].id == tid]
            if asked != at[tid][: len(asked)]:
                raise ToolError(
                    f"'{listing[asked[0] - 1].title}' is at positions {at[tid]}, and Apple "
                    f"removes the topmost copy first. Ask for positions {at[tid][: len(asked)]} "
                    "instead (same song, so the playlist ends up the same apart from order)."
                )
    else:
        ids = list(track_ids)
        missing = [t for t in dict.fromkeys(ids) if ids.count(t) > len(at.get(t, []))]
        if missing:
            raise ToolError(
                f"Not in the playlist (or fewer copies than asked): {', '.join(missing)}."
            )
    taken: dict[str, int] = {}
    removed = []
    for tid in ids:  # which copy each removal takes: the topmost not yet taken
        pos = at[tid][taken.get(tid, 0)]
        taken[tid] = taken.get(tid, 0) + 1
        removed.append(_change(pos, listing[pos - 1]))

    with _apple_errors():
        result = await library().remove_tracks(playlist_id, ids)
    if result.stopped:
        done = len(ids) - len(result.failed)
        raise ToolError(
            f"Removal stopped partway ({result.stopped}); about {done} of {len(ids)} went. "
            "Positions have shifted: call get_playlist before removing more."
        )
    if result.tracks is None:
        raise ToolError(
            f"Apple accepted the removal, but reading the playlist back failed ({result.reason}). "
            "Check get_playlist before retrying; a retry removes another copy."
        )
    if not result.ok:
        raise ToolError(
            f"Apple accepted the removal, but after several seconds the playlist still shows "
            f"{', '.join(result.failed)}. It may yet update: check get_playlist before "
            "retrying; a retry can remove another copy."
        )
    return PlaylistChange(
        summary=f"Removed {len(ids)} songs from {playlist_id} via the {result.rail} rail; "
        "re-read and confirmed.",
        playlist_id=playlist_id,
        rail=result.rail,
        confirmation="read_back",
        removed=removed,
        track_count_after=len(result.tracks),
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Sign-in link",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
async def sign_in_link(service: Literal["apple"] = "apple") -> SignInLink:
    """[apple-music] A single-use link to the Apple Music sign-in page, valid for
    15 minutes. Give the URL to the user to open in their browser; don't open or
    fetch it yourself, since the first visit uses it up. If they see "expired",
    call this again."""
    url = signin.link_url(public_url(), service, store().issue_link(service))
    return SignInLink(
        summary="Give this link to the user to open in a browser within 15 minutes; it works once.",
        url=url,
        expires_in_seconds=LINK_TTL_SECONDS,
    )


# -- entry points -------------------------------------------------------------


# -- mirrors --------------------------------------------------------------------


class UnavailableTrack(BaseModel):
    title: str
    artists: str
    album: str | None = None
    spotify_id: str | None = None
    isrc: str | None = None
    reason: str


class MirrorInfo(BaseModel):
    name: str
    source: str = Field(description="Where tracks come from.")
    playlist_id: str | None = Field(description="None until the first sync sets it up.")
    window_tracks: int = Field(description="Occurrences in the window now.")
    window_hours: float = Field(description="Their total duration.")
    window_limit_hours: float
    last_sync: str | None
    last_error: str | None = Field(
        description="The last failure, if it came after the last successful sync."
    )
    coverage: float | None = Field(
        description="Music received per hour of wall time over the last 24 h; near "
        "1.0 when nothing is missed. None in the first hour."
    )
    possibly_missing_tracks: bool = Field(
        description="Coverage is below the threshold after a full day."
    )
    shortest_gap_seconds: int | None = Field(
        description="Shortest time between two arrivals; below the feed's poll "
        "interval, a track could slip between polls."
    )
    unavailable: list[UnavailableTrack] = Field(
        description="Tracks in the window that Apple Music lacks; left out."
    )
    ambiguous: list[str] = Field(
        description="Title matches with several Apple candidates; the first was used."
    )
    recent_repairs: list[str]


class MirrorStatus(BaseModel):
    summary: str
    enabled: bool = Field(description="Whether the loop runs (MIRRORS_ENABLED).")
    config_error: str | None = None
    mirrors: list[MirrorInfo] = []


class MirrorSync(BaseModel):
    mirror: str
    playlist_id: str | None = None
    added: list[str] = []
    removed: list[str] = []
    failed: list[str] = []
    unavailable: list[str] = []
    repairs: list[str] = []
    notes: list[str] = []
    error: str | None = None


class SyncReport(BaseModel):
    summary: str
    results: list[MirrorSync]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _selected(name: str | None) -> list[Mirror]:
    try:
        configured = mirrors()
    except ConfigError as e:
        raise ToolError(f"The mirrors file is invalid: {e}") from e
    if not name:
        return configured
    chosen = [m for m in configured if m.name == name.casefold()]
    if not chosen:
        names = ", ".join(m.name for m in configured) or "none configured"
        raise ToolError(f"No mirror named {name!r} (mirrors: {names}).")
    return chosen


def _mirror_info(m: Mirror) -> MirrorInfo:
    st = store()
    row = st.mirror(m.name)
    entries = st.entries(m.name)
    live = [e for e in entries if not e["expired"]]
    now = datetime.now(timezone.utc).timestamp()
    cov, gap = coverage(st, m, now)
    full_day = bool(row) and now - row["created_at"] >= 86_400
    source = f"feed: {m.source.url}" if m.source.type == "feed" else m.source.type
    return MirrorInfo(
        name=m.name,
        source=source,
        playlist_id=row["playlist_id"] if row else None,
        window_tracks=len(live),
        window_hours=round(sum(e["duration_ms"] or 0 for e in live) / 3_600_000, 2),
        window_limit_hours=round(m.window_seconds / 3600, 2),
        last_sync=_iso(row["last_sync"]) if row else None,
        last_error=row["last_error"] if _failing(row) else None,
        coverage=round(cov, 3) if cov is not None else None,
        possibly_missing_tracks=full_day
        and cov is not None
        and cov < settings.mirror_coverage_threshold,
        shortest_gap_seconds=gap,
        unavailable=[
            UnavailableTrack(
                title=e["title"],
                artists=e["artists"],
                album=e["album"],
                spotify_id=e["spotify_id"],
                isrc=e["isrc"],
                reason=e["unavailable_reason"],
            )
            for e in live
            if e["unavailable_reason"]
        ],
        ambiguous=[
            f"{e['title']} – {e['artists']} ({e['apple_song_id']})"
            for e in live
            if e["match_method"] == "title-ambiguous"
        ],
        recent_repairs=[
            f"{_iso(r['at'])} {r['action']}: {r['detail']}"
            for r in st.repairs(m.name, 10)
        ],
    )


@mcp.tool(tags=_TAGS, annotations=ToolAnnotations(title="Mirror status", **_READ))
async def mirror_status(name: str | None = None) -> MirrorStatus:
    """[apple-music] Each mirror's source, playlist, window, last sync and error,
    source coverage, tracks Apple lacks, ambiguous matches and recent repairs.
    Omit `name` for all mirrors."""
    try:
        chosen = _selected(name)
    except ToolError as e:
        if name:
            raise
        return MirrorStatus(
            summary=str(e), enabled=settings.mirrors_enabled, config_error=str(e)
        )
    infos = [_mirror_info(m) for m in chosen]
    failing = [i.name for i in infos if i.last_error]
    state = "running" if settings.mirrors_enabled else "off (MIRRORS_ENABLED=false)"
    summary = f"{len(infos)} mirrors, loop {state}." + (
        f" Failing: {', '.join(failing)}." if failing else ""
    )
    return MirrorStatus(
        summary=summary, enabled=settings.mirrors_enabled, mirrors=infos
    )


@mcp.tool(
    tags=_TAGS,
    annotations=ToolAnnotations(
        title="Sync mirrors now",
        read_only_hint=False,
        destructive_hint=True,  # removes expired and stray tracks
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def sync_mirror(name: str | None = None) -> SyncReport:
    """[apple-music] Poll and sync one mirror, or all when `name` is omitted, and
    report the tracks added, removed and failed. Needs the loop on."""
    if not settings.mirrors_enabled:
        raise ToolError(
            "Mirrors are off (MIRRORS_ENABLED=false), so nothing is written. "
            "Set MIRRORS_ENABLED=true and restart to run them."
        )
    loop = scheduler()
    results = []
    with _apple_errors():
        for m in _selected(name):
            await loop.poll(m)
            try:
                r = await loop.sync(m)
            except (UserTokenMissing, UserTokenRejected):
                raise
            except AppleError as e:
                results.append(MirrorSync(mirror=m.name, error=str(e)))
                continue
            results.append(MirrorSync(**vars(r)))
    added = sum(len(r.added) for r in results)
    removed = sum(len(r.removed) for r in results)
    errors = [r.mirror for r in results if r.error]
    summary = f"Synced {len(results)} mirrors: {added} added, {removed} removed." + (
        f" Failed: {', '.join(errors)}." if errors else ""
    )
    return SyncReport(summary=summary, results=results)


def _change_line(r) -> str:
    detail = f"({r['detail']})" if r["detail"] else ""
    return " ".join(x for x in (_iso(r["at"]), r["action"], r["title"], detail) if x)


class MirrorAudit(BaseModel):
    name: str
    playlist_id: str | None
    playlist_tracks: int | None = Field(description="Read from Apple now.")
    expected_tracks: int = Field(
        description="Copies the window says the playlist holds (expiring ones included)."
    )
    in_step: bool | None = Field(
        description="playlist_tracks == expected_tracks; None if Apple couldn't be read."
    )
    window_tracks: int
    window_hours: float
    coverage: float | None
    counts: dict[str, int] = Field(
        description="Changes in the period by kind: added, removed, skipped, repair, "
        "error, adopted, created, drift refused."
    )
    per_day: dict[str, dict[str, int]] = Field(description="The same, per UTC day.")
    errors: list[str] = Field(description="Failures in the period, newest first.")
    recent: list[str] = Field(description="The latest changes, newest first.")


class AuditReport(BaseModel):
    summary: str
    hours: int
    mirrors: list[MirrorAudit]


@mcp.tool(tags=_TAGS, annotations=ToolAnnotations(title="Audit mirrors", **_READ))
async def mirror_audit(
    name: str | None = None, hours: Annotated[int, Field(ge=1, le=720)] = 72
) -> AuditReport:
    """[apple-music] What each mirror did over the last `hours` (default 72, at
    most 30 days): counts of tracks added, removed and skipped, repairs and
    errors, per day, the latest changes, and whether the playlist on Apple holds
    exactly what the window expects (`in_step`). Omit `name` for all mirrors."""
    st = store()
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    out = []
    for mirror in _selected(name):
        row = st.mirror(mirror.name)
        entries = st.entries(mirror.name)
        live = [e for e in entries if not e["expired"]]
        expected = sum(
            1
            for e in entries
            if e["placed"] and e["apple_song_id"] and not e["unavailable_reason"]
        )
        listed = None
        if row and row["playlist_id"]:
            with contextlib.suppress(AppleError, ToolError):
                listed = len(
                    (await apple_api().playlist_tracks(row["playlist_id"])).data
                )
        counts: dict[str, int] = {}
        per_day: dict[str, dict[str, int]] = {}
        for r in st.history_counts(mirror.name, since):
            counts[r["action"]] = counts.get(r["action"], 0) + r["n"]
            per_day.setdefault(r["day"], {})[r["action"]] = r["n"]
        changes = st.history(mirror.name, since, 200)
        out.append(
            MirrorAudit(
                name=mirror.name,
                playlist_id=row["playlist_id"] if row else None,
                playlist_tracks=listed,
                expected_tracks=expected,
                in_step=None if listed is None else listed == expected,
                window_tracks=len(live),
                window_hours=round(
                    sum(e["duration_ms"] or 0 for e in live) / 3_600_000, 2
                ),
                coverage=coverage(st, mirror, since + hours * 3600)[0],
                counts=counts,
                per_day=per_day,
                errors=[_change_line(r) for r in changes if r["action"] == "error"][
                    :20
                ],
                recent=[_change_line(r) for r in changes[:30]],
            )
        )
    off = [a.name for a in out if a.in_step is False]
    summary = (
        f"{len(out)} mirrors over {hours} h: "
        + ", ".join(
            f"{a.name} +{a.counts.get('added', 0)}/-{a.counts.get('removed', 0)}"
            + (f", {a.counts['error']} errors" if a.counts.get("error") else "")
            for a in out
        )
        + (f". Out of step: {', '.join(off)}." if off else ".")
    )
    return AuditReport(summary=summary, hours=hours, mirrors=out)


def main(argv: list[str] | None = None) -> None:
    """Entry point: run the server, or `sign-in-link apple` to print a sign-in link."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        if args != ["sign-in-link", "apple"]:
            sys.exit("usage: mcp-apple-music [sign-in-link apple]")
        print(signin.link_url(public_url(), "apple", store().issue_link("apple")))
        return

    if settings.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=settings.host,
            port=settings.port,
            stateless_http=True,
            middleware=http_middleware(),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
