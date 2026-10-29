"""The Apple sign-in routes, the CLI that issues links, and what /health reports."""

from __future__ import annotations

import json

import httpx
import pytest

import mcp_apple_music.server as srv
from mcp_apple_music.apple_api import AppleMusicAPI
from mcp_apple_music.config import settings
from mcp_apple_music.store import APPLE_USER_TOKEN

from .fakes import Apple


@pytest.fixture
def apple() -> Apple:
    return Apple()


@pytest.fixture
def configured(monkeypatch, keypair, apple):
    """MusicKit key set, and the documented rail pointed at the scripted Apple."""
    monkeypatch.setattr(settings, "apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "apple_key_id", "KEY")
    monkeypatch.setattr(
        settings, "apple_private_key", type(settings.apple_private_key)(keypair[0])
    )
    client = AppleMusicAPI(
        srv.developer_token(),
        user_token=lambda: srv.store().valid_token(APPLE_USER_TOKEN),
        on_user_token_rejected=lambda: srv.store().mark_token_invalid(APPLE_USER_TOKEN),
        transport=httpx.MockTransport(apple),
    )
    monkeypatch.setattr(srv, "apple_api", lambda: client)
    return client


@pytest.fixture
async def http():
    transport = httpx.ASGITransport(app=srv.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def health(http) -> dict:
    return (await http.get("/health")).json()["credentials"]


async def sign_in(http, link: str, mut: str = "MUT-NEW") -> httpx.Response:
    return await http.post("/auth/apple", json={"t": link, "music_user_token": mut})


async def test_page_needs_musickit_key_and_keeps_the_link(http, monkeypatch, keypair):
    link = srv.store().issue_link("apple")
    assert (await http.get("/auth/apple", params={"t": link})).status_code == 503
    monkeypatch.setattr(settings, "apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "apple_key_id", "KEY")
    monkeypatch.setattr(
        settings, "apple_private_key", type(settings.apple_private_key)(keypair[0])
    )
    srv.developer_token.cache_clear()
    assert (await http.get("/auth/apple", params={"t": link})).status_code == 200


async def test_page_serves_musickit_once(http, configured):
    link = srv.store().issue_link("apple")
    page = await http.get("/auth/apple", params={"t": link})
    assert page.status_code == 200
    assert "musickit/v3/musickit.js" in page.text
    assert f'"link": "{link}"' in page.text and '"dev": "ey' in page.text
    assert page.headers["Referrer-Policy"] == "origin"
    assert page.headers["Cache-Control"] == "no-store"

    again = await http.get("/auth/apple", params={"t": link})
    assert again.status_code == 403 and "already used or has expired" in again.text
    assert (await http.get("/auth/apple")).status_code == 403  # no link at all


async def test_sign_in_stores_token_and_health_shows_it(http, configured, apple):
    assert (await health(http))["apple_user_token"]["state"] == "missing"
    link = srv.store().issue_link("apple")
    await http.get("/auth/apple", params={"t": link})

    resp = await sign_in(http, link)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "message": "Signed in (storefront nl). You can close this page.",
    }
    assert apple.sent("/v1/me/storefront")[0].headers["Music-User-Token"] == "MUT-NEW"

    creds = await health(http)
    assert creds["developer_token"]["state"] == "valid"
    assert creds["apple_user_token"]["state"] == "valid"
    assert creds["apple_user_token"]["obtained_at"]
    assert "MUT-NEW" not in json.dumps(creds)

    assert (await sign_in(http, link)).status_code == 403  # completed links are spent


async def test_sign_in_replaces_an_invalid_token(http, configured):
    srv.store().set_token(APPLE_USER_TOKEN, "MUT-OLD")
    srv.store().mark_token_invalid(APPLE_USER_TOKEN)
    assert (await health(http))["apple_user_token"]["state"] == "invalid"
    link = srv.store().issue_link("apple")
    await http.get("/auth/apple", params={"t": link})
    assert (await sign_in(http, link)).status_code == 200
    assert srv.store().valid_token(APPLE_USER_TOKEN) == "MUT-NEW"


async def test_sign_in_rejected_by_apple(http, configured, apple):
    apple.routes[("GET", "/v1/me/storefront")] = [
        httpx.Response(403, json={"errors": [{"title": "Forbidden"}]})
    ]
    link = srv.store().issue_link("apple")
    await http.get("/auth/apple", params={"t": link})
    resp = await sign_in(http, link)
    assert resp.status_code == 400 and not resp.json()["ok"]
    assert (await health(http))["apple_user_token"]["state"] == "invalid"


async def test_post_without_opening_the_page_is_refused(http, configured):
    link = srv.store().issue_link("apple")
    assert (await sign_in(http, link)).status_code == 403
    assert srv.store().token(APPLE_USER_TOKEN) is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{}",
        b'{"t": "x"}',
        b'{"t": 1, "music_user_token": "m"}',
        b'{"t": "x", "music_user_token": ""}',
    ],
)
async def test_malformed_post(http, configured, body):
    resp = await http.post("/auth/apple", content=body)
    assert resp.status_code == 400


async def test_oversized_post(http, configured):
    resp = await http.post("/auth/apple", content=b"x" * (srv.MAX_BODY_BYTES + 1))
    assert resp.status_code == 413


async def test_cli_prints_a_working_link(http, configured, capsys, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://music.example.org/")
    srv.main(["sign-in-link", "apple"])
    url = httpx.URL(capsys.readouterr().out.strip())
    assert (url.scheme, url.host, url.path) == (
        "https",
        "music.example.org",
        "/auth/apple",
    )
    assert (
        await http.get("/auth/apple", params={"t": url.params["t"]})
    ).status_code == 200


def test_cli_default_url_and_usage(capsys):
    srv.main(["sign-in-link", "apple"])
    assert capsys.readouterr().out.startswith(
        f"http://127.0.0.1:{settings.port}/auth/apple?t="
    )
    with pytest.raises(SystemExit):
        srv.main(["sign-in-link", "spotify"])


async def test_sign_in_runs_the_canary_at_once(http, configured, monkeypatch):
    """So /health reports both rails right after sign-in, not a day later."""
    import asyncio

    ran = asyncio.Event()

    async def fake_canary() -> None:
        ran.set()

    monkeypatch.setattr(settings, "canary_interval_seconds", 86_400)
    monkeypatch.setattr(srv, "run_canary", fake_canary)
    link = srv.store().issue_link("apple")
    await http.get("/auth/apple", params={"t": link})
    assert (await sign_in(http, link)).status_code == 200
    await asyncio.wait_for(ran.wait(), timeout=2)
