"""/health: credential states, canary, mirrors, overall status, and no secrets."""

from __future__ import annotations

import time

import httpx
import pytest

import mcp_apple_music.server as srv
from mcp_apple_music.config import settings
from mcp_apple_music.store import APPLE_USER_TOKEN

SECRETS = {
    "mcp_api_key": "api-key-do-not-show",
    "webhook_secret": "webhook-secret-do-not-show",
    "spotify_client_secret": "spotify-secret-do-not-show",
}


@pytest.fixture
def configured(monkeypatch, keypair):
    monkeypatch.setattr(settings, "apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "apple_key_id", "KEY")
    monkeypatch.setattr(
        settings, "apple_private_key", type(settings.apple_private_key)(keypair[0])
    )
    monkeypatch.setattr(settings, "spotify_client_id", "spotify-id")
    for name, value in SECRETS.items():
        monkeypatch.setattr(settings, name, type(getattr(settings, name))(value))
    srv.store().set_token(APPLE_USER_TOKEN, "MUT-do-not-show")
    srv.store().set_check(srv.CANARY, True, "probe song 1; created p.1")


async def health() -> dict:
    transport = httpx.ASGITransport(app=srv.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return (await c.get("/health")).json()


async def test_healthy(configured):
    body = await health()
    assert (body["status"], body["problems"]) == ("healthy", [])
    creds = body["credentials"]
    assert {k: v["state"] for k, v in creds.items()} == {
        "developer_token": "valid",
        "web_player_token": "not fetched yet",
        "apple_user_token": "valid",
        "spotify_app": "configured",
        "spotify_owner_login": "not used",
    }
    assert body["canary"]["state"] == "ok"


async def test_expiring_user_token_degrades(configured):
    srv.store()._db.execute(
        "UPDATE tokens SET obtained_at = ? WHERE name = ?",
        (time.time() - 151 * 86_400, APPLE_USER_TOKEN),
    )
    body = await health()
    assert body["credentials"]["apple_user_token"]["state"] == "expiring soon"
    assert body["status"] == "degraded"
    assert body["problems"] == ["Apple Music sign-in expiring soon: sign in again"]


async def test_invalid_user_token_and_failed_canary(configured):
    srv.store().mark_token_invalid(APPLE_USER_TOKEN)
    srv.store().set_check(
        srv.CANARY, False, "remove: expected 1 of 2 copies left, found 0"
    )
    body = await health()
    assert body["status"] == "degraded"
    assert body["problems"] == [
        "Apple Music sign-in invalid: sign in again",
        "canary failed",
    ]
    assert body["canary"]["error"].startswith("remove: expected 1 of 2")


async def test_missing_sign_in_is_named(configured):
    srv.store()._db.execute("DELETE FROM tokens")
    assert (await health())["problems"] == ["Apple Music sign-in needed"]


async def test_no_secret_appears(configured, keypair):
    raw = str(await health())
    for value in [*SECRETS.values(), "MUT-do-not-show", keypair[0].splitlines()[1]]:
        assert value not in raw


async def test_canary_run_is_recorded_and_skipped_without_sign_in(
    configured, monkeypatch
):
    from mcp_apple_music.canary import CanaryResult

    async def fake_run(library):
        return CanaryResult(
            ok=False, steps=["probe song 1"], error="append: not present"
        )

    monkeypatch.setattr(srv.canary, "run", fake_run)
    monkeypatch.setattr(srv, "library", lambda: None)
    await srv.run_canary()
    assert srv.store().check(srv.CANARY)["detail"] == "append: not present"
    srv.store()._db.execute("DELETE FROM tokens")
    srv.store().set_check(srv.CANARY, True, "earlier")
    await srv.run_canary()  # nobody signed in: not run, not failed
    assert srv.store().check(srv.CANARY)["ok"] == 1


async def test_unknown_paths_need_the_key(configured):
    app = srv.mcp.http_app(middleware=srv.http_middleware())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/admin")).status_code == 401
        assert (
            await c.get("/hooks")
        ).status_code == 401  # only /hooks/<secret> is open
        assert (await c.get("/health")).status_code == 200
        assert (
            await c.post("/hooks/wrong", json={})
        ).status_code == 404  # its own gate
        assert (await c.get("/auth/apple")).status_code == 403  # its own gate
        key = {"authorization": f"Bearer {SECRETS['mcp_api_key']}"}
        assert (await c.get("/admin", headers=key)).status_code == 404


def test_mcp_needs_the_bearer_when_a_key_is_set(tmp_path):
    """Auth is wired at import from MCP_API_KEY, so check it in a fresh process."""
    import os
    import subprocess
    import sys

    probe = """
import asyncio, httpx
import mcp_apple_music.server as srv
async def main():
    app = srv.mcp.http_app(middleware=srv.http_middleware())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        print((await c.post("/mcp", json={})).status_code, (await c.get("/x")).status_code)
asyncio.run(main())
"""
    env = {
        **os.environ,
        "MCP_API_KEY": "k",
        "TRANSPORT": "http",
        "DATA_DIR": str(tmp_path),
    }
    out = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.split() == ["401", "401"], out.stderr[-500:]
