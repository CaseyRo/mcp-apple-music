"""MCP server for Apple Music."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import BearerTokenVerifier
from .config import settings
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


# -- health -------------------------------------------------------------------


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
        }
    )


# -- entry points -------------------------------------------------------------


def main() -> None:
    """Entry point for the mcp-apple-music server."""
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
