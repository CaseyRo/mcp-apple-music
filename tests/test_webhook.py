"""POST /hooks/<secret>: one endpoint for every mirror, answering before any work."""

from __future__ import annotations

import time

import httpx
import pytest

import mcp_apple_music.server as srv
from mcp_apple_music.config import settings
from mcp_apple_music.mirror import Track

SECRET = "s3cret-path-token"
EXAMPLE = {
    "playlist": "music",
    "name": "The Fez",
    "artists": "Steely Dan",
    "album": "The Royal Scam",
}
MIRRORS = """
mirrors:
  - name: music
    playlist_name: Station Music
    source: {type: feed, url: "https://radio.example/api/music.json"}
  - name: rock
    playlist_name: Station Rock
    webhook_key: ROCK
    source: {type: webhook}
"""


@pytest.fixture
def configured(monkeypatch, keypair, tmp_path):
    (tmp_path / "mirrors.yaml").write_text(MIRRORS)
    monkeypatch.setattr(settings, "mirrors_file", tmp_path / "mirrors.yaml")
    monkeypatch.setattr(
        settings, "webhook_secret", type(settings.webhook_secret)(SECRET)
    )
    monkeypatch.setattr(settings, "apple_team_id", "TEAM")
    monkeypatch.setattr(settings, "apple_key_id", "KEY")
    monkeypatch.setattr(
        settings, "apple_private_key", type(settings.apple_private_key)(keypair[0])
    )


@pytest.fixture
async def http():
    transport = httpx.ASGITransport(app=srv.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def titles(mirror: str) -> list[str]:
    return [e["title"] for e in srv.store().entries(mirror)]


async def test_valid_push_is_accepted_within_a_second(http, configured):
    start = time.monotonic()
    resp = await http.post(f"/hooks/{SECRET}", json=EXAMPLE)
    assert time.monotonic() - start < 1
    assert (resp.status_code, resp.json()) == (202, {"accepted": True, "new": True})
    assert titles("music") == ["The Fez"]


async def test_key_is_case_insensitive_and_can_differ_from_the_name(http, configured):
    resp = await http.post(f"/hooks/{SECRET}", json={**EXAMPLE, "playlist": "rock"})
    assert resp.status_code == 202 and titles("rock") == ["The Fez"]


async def test_no_content_type_needed(http, configured):
    """Sent as text/plain, the body is still read as JSON."""
    resp = await http.post(
        f"/hooks/{SECRET}",
        content=b'{"playlist":"music","name":"The Fez","artists":"Steely Dan"}',
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 202


async def test_wrong_secret_or_disabled_is_404(http, configured, monkeypatch):
    assert (await http.post("/hooks/guess", json=EXAMPLE)).status_code == 404
    monkeypatch.setattr(settings, "webhook_secret", type(settings.webhook_secret)(""))
    assert (await http.post("/hooks/", json=EXAMPLE)).status_code == 404
    assert (await http.post(f"/hooks/{SECRET}", json=EXAMPLE)).status_code == 404
    assert titles("music") == []


async def test_unknown_playlist_is_422_naming_it(http, configured):
    resp = await http.post(f"/hooks/{SECRET}", json={**EXAMPLE, "playlist": "polka"})
    assert resp.status_code == 422 and "'polka'" in resp.json()["error"]
    assert titles("music") == []


async def test_bad_bodies(http, configured):
    assert (
        await http.post(f"/hooks/{SECRET}", content=b"x" * 20_000)
    ).status_code == 413
    assert (await http.post(f"/hooks/{SECRET}", content=b"{nope")).status_code == 400
    resp = await http.post(f"/hooks/{SECRET}", json={"playlist": "music"})
    assert resp.status_code == 400 and "needs an `id`" in resp.json()["error"]


async def test_invalid_mirrors_file_is_503(http, configured, tmp_path):
    (tmp_path / "mirrors.yaml").write_text("mirrors: [{name: music}]")
    resp = await http.post(f"/hooks/{SECRET}", json=EXAMPLE)
    assert resp.status_code == 503 and "mirror 1 (music)" in resp.json()["error"]


async def test_webhook_then_feed_is_one_delivery(http, configured):
    """The push has no Spotify ID; the feed reports the same track with one."""
    await http.post(f"/hooks/{SECRET}", json=EXAMPLE)
    music = srv.mirrors()[0]
    from_feed = Track(
        "The Fez", "Steely Dan", "The Royal Scam", "4uLU6hMCjMI75M1A2tKUQC", 241_000
    )
    assert srv.engine().ingest(music, [from_feed], "feed") == 0
    assert titles("music") == ["The Fez"]
