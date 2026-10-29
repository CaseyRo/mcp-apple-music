"""mirror_status, sync_mirror and the mirrors block in /health."""

from __future__ import annotations

import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import mcp_apple_music.server as srv
from mcp_apple_music.config import settings
from mcp_apple_music.mirror import Engine, Track
from mcp_apple_music.scheduler import Scheduler

from .test_mirror import FakeApple, Matcher

MIRRORS = """
mirrors:
  - name: rock
    playlist_name: Station Rock
    window_seconds: 300
    source: {type: webhook}
  - name: soul
    playlist_name: Station Soul
    source: {type: webhook}
"""


@pytest.fixture
def apple() -> FakeApple:
    return FakeApple()


@pytest.fixture
def matcher() -> Matcher:
    return Matcher()


@pytest.fixture
def configured(monkeypatch, tmp_path, apple, matcher):
    (tmp_path / "mirrors.yaml").write_text(MIRRORS)
    monkeypatch.setattr(settings, "mirrors_file", tmp_path / "mirrors.yaml")
    monkeypatch.setattr(settings, "mirrors_enabled", True)
    engine = Engine(srv.store(), apple, matcher)
    monkeypatch.setattr(srv, "engine", lambda: engine)

    async def idle(self) -> None:  # the client's lifespan would start the real loop
        pass

    monkeypatch.setattr(Scheduler, "run", idle)
    return engine


async def call(name: str, args: dict | None = None) -> dict:
    async with Client(srv.mcp) as c:
        return (await c.call_tool(name, args or {})).structured_content


def push(engine, *titles: str) -> None:
    rock = srv.mirrors()[0]
    engine.ingest(
        rock,
        [
            Track(t, "Artist", spotify_id=f"{t:0>22}", duration_ms=60_000)
            for t in titles
        ],
        "webhook",
    )


async def test_sync_now_lists_what_changed(configured, apple):
    push(configured, "A", "B")
    out = await call("sync_mirror", {"name": "rock"})
    (r,) = out["results"]
    assert (r["added"], r["removed"], r["failed"], r["error"]) == (
        ["A – Artist", "B – Artist"],
        [],
        [],
        None,
    )
    assert out["summary"] == "Synced 1 mirrors: 2 added, 0 removed."


async def test_status_for_one_mirror(configured, apple, matcher):
    matcher.missing = {"B"}
    push(configured, "A", "B", "C")
    await call("sync_mirror", {"name": "rock"})
    pid = srv.store().mirror("rock")["playlist_id"]
    apple.playlists[pid].append("X")  # added by hand
    await call("sync_mirror", {"name": "rock"})
    out = await call("mirror_status", {"name": "rock"})
    (info,) = out["mirrors"]
    assert (info["window_tracks"], info["window_hours"], info["playlist_id"]) == (
        3,
        0.05,
        pid,
    )
    assert info["last_sync"] and info["last_error"] is None
    assert [u["title"] for u in info["unavailable"]] == ["B"]
    assert (
        info["unavailable"][0]["reason"]
        == "no ISRC match and no acceptable title match"
    )
    assert info["recent_repairs"][0].endswith("removed stray: X (X)")


async def test_unavailable_report_survives_a_restart(
    configured, matcher, monkeypatch, apple
):
    matcher.missing = {"B"}
    push(configured, "A", "B")
    await call("sync_mirror", {"name": "rock"})
    srv.store().close()
    srv.store.cache_clear()  # a new process: state comes back from the file
    monkeypatch.setattr(srv, "engine", lambda: Engine(srv.store(), apple, matcher))
    (info,) = (await call("mirror_status", {"name": "rock"}))["mirrors"]
    assert [u["title"] for u in info["unavailable"]] == ["B"]


async def test_sync_is_refused_while_mirrors_are_off(configured, monkeypatch):
    monkeypatch.setattr(settings, "mirrors_enabled", False)
    with pytest.raises(ToolError, match="MIRRORS_ENABLED=false"):
        await call("sync_mirror")
    assert "loop off" in (await call("mirror_status"))["summary"]


async def test_unknown_mirror(configured):
    with pytest.raises(
        ToolError, match=r"No mirror named 'jazz' \(mirrors: rock, soul\)"
    ):
        await call("mirror_status", {"name": "jazz"})


async def test_coverage_flags_missed_tracks_after_a_full_day(configured):
    st, now = srv.store(), time.time()
    st.bind_mirror("rock", "p.1")
    st._db.execute(
        "UPDATE mirrors SET created_at = ? WHERE name = 'rock'", (now - 2 * 86_400,)
    )
    for i in range(48):  # 48 half-hour tracks in a day would be complete; send 36
        if i % 4:
            st._db.execute(
                "INSERT INTO arrivals (mirror, key, at, duration_ms, source) VALUES ('rock', ?, ?, ?, 'feed')",
                (f"k{i}", now - 86_000 + i * 1790, 1_800_000),
            )
    (info,) = (await call("mirror_status", {"name": "rock"}))["mirrors"]
    assert 0.7 < info["coverage"] < 0.8 and info["possibly_missing_tracks"]
    assert info["shortest_gap_seconds"] == 1790


async def test_health_names_an_invalid_entry(configured, tmp_path):
    (tmp_path / "mirrors.yaml").write_text(
        "mirrors:\n  - name: soul\n    playlist_name: S\n"
    )
    srv.mirrors.cache_clear()
    transport = httpx.ASGITransport(app=srv.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = (await c.get("/health")).json()
    assert body["mirrors"]["config_error"].startswith("mirror 1 (soul): source")
    out = await call("mirror_status")
    assert out["config_error"].startswith(
        "The mirrors file is invalid: mirror 1 (soul)"
    )


async def test_loop_runs_while_the_server_runs(monkeypatch, tmp_path, apple, matcher):
    import asyncio

    (tmp_path / "mirrors.yaml").write_text(MIRRORS)
    monkeypatch.setattr(settings, "mirrors_file", tmp_path / "mirrors.yaml")
    monkeypatch.setattr(settings, "mirrors_enabled", True)
    engine = Engine(srv.store(), apple, matcher)
    monkeypatch.setattr(srv, "engine", lambda: engine)

    async def until(check) -> None:
        for _ in range(100):
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the loop did not get there")

    async with Client(srv.mcp):  # enters the server lifespan
        await until(lambda: len(apple.playlists) == 2)  # first sync sets both up
        push(engine, "A")
        srv.scheduler().wake("rock")  # what the webhook does after storing
        await until(lambda: apple.playlists["p.1"] == ["A"])
    assert not srv.scheduler()._running  # stopped with the server


async def test_audit_counts_changes_and_checks_the_playlist(
    configured, apple, matcher, monkeypatch
):
    monkeypatch.setattr(srv, "apple_api", lambda: apple)
    matcher.missing = {"B"}
    push(configured, "A", "B", "C")
    await call("sync_mirror", {"name": "rock"})
    out = await call("mirror_audit", {"name": "rock", "hours": 24})
    (a,) = out["mirrors"]
    assert a["counts"] == {"created": 1, "skipped": 1, "added": 2}
    assert (a["playlist_tracks"], a["expected_tracks"], a["in_step"]) == (2, 2, True)
    assert a["recent"][0].split(" ", 1)[1].startswith("added C – Artist")
    pid = srv.store().mirror("rock")["playlist_id"]
    apple.playlists[pid].append("X")  # someone edits the playlist by hand
    (a,) = (await call("mirror_audit", {"name": "rock"}))["mirrors"]
    assert a["in_step"] is False
    assert "Out of step: rock" in (await call("mirror_audit"))["summary"]
