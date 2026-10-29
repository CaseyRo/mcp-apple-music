"""The MCP surface over the wire, via an in-memory ``fastmcp.Client``.

These tests check what a connector sees: the server stands up, every expected tool is
registered, and the public health route answers. CI sets
FASTMCP_MCP_CAMELCASE_COMPAT=false, so snake_case is what the client reads.
"""

from __future__ import annotations

import httpx
from fastmcp import Client

import mcp_apple_music.server as srv

# Grows as tools land; every tool the server ships must be listed here.
EXPECTED_TOOLS: set[str] = {
    "search",
    "lookup_isrc",
    "list_playlists",
    "get_playlist",
    "create_playlist",
    "rename_playlist",
    "delete_playlist",
    "add_tracks",
    "remove_tracks",
    "sign_in_link",
    "mirror_status",
    "sync_mirror",
}


async def test_server_registers_its_tools():
    async with Client(srv.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"
    assert names <= EXPECTED_TOOLS, f"unlisted tools: {names - EXPECTED_TOOLS}"


async def test_health_answers():
    app = srv.mcp.http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "mcp-apple-music"
    assert body["status"] == "degraded"  # nothing configured in tests
    assert "MusicKit key missing" in body["problems"]


async def test_write_tools_carry_annotations():
    async with Client(srv.mcp) as client:
        tools = {t.name: t.annotations for t in await client.list_tools()}
    for name in ("delete_playlist", "remove_tracks", "sync_mirror"):
        assert tools[name].destructive_hint is True, name
    for name in (
        "search",
        "lookup_isrc",
        "list_playlists",
        "get_playlist",
        "mirror_status",
    ):
        assert tools[name].read_only_hint is True, name
