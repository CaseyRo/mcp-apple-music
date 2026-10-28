"""MCP server for Apple Music."""

from __future__ import annotations

import functools
import json
import os
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import signin
from .apple_api import AppleMusicAPI
from .auth import BearerTokenVerifier
from .config import settings
from .devtoken import DeveloperToken
from .rails import AppleError, UserTokenRejected
from .store import APPLE_USER_TOKEN, Store
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

mcp = FastMCP("mcp-apple-music", auth=_auth)
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
        raise RuntimeError("MusicKit key not configured")
    return AppleMusicAPI(
        dev,
        user_token=lambda: store().valid_token(APPLE_USER_TOKEN),
        on_user_token_rejected=lambda: store().mark_token_invalid(APPLE_USER_TOKEN),
    )


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


def _user_token_state() -> dict:
    tok = store().token(APPLE_USER_TOKEN)
    if tok is None:
        return {"state": "missing", "obtained_at": None}
    return {
        "state": "invalid" if tok.invalid else "valid",
        "obtained_at": datetime.fromtimestamp(
            tok.obtained_at, timezone.utc
        ).isoformat(),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Public health endpoint."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "mcp-apple-music",
            "version": __version__,
            "uptime_seconds": int(
                (datetime.now(timezone.utc) - _start_time).total_seconds()
            ),
            "credentials": {
                "musickit_key": "configured" if developer_token() else "missing",
                "apple_user_token": _user_token_state(),
            },
        }
    )


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
    return answer(200, f"Signed in (storefront {storefront}). You can close this page.")


# -- entry points -------------------------------------------------------------


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
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
