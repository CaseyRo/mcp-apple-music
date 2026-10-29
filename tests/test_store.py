"""SQLite state: user tokens and single-use sign-in links."""

from __future__ import annotations

import sqlite3

import pytest

from mcp_apple_music.store import APPLE_USER_TOKEN, LINK_TTL_SECONDS, Store

from .fakes import Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "nested" / "state.db", clock=clock)
    yield s
    s.close()


def test_user_token_lifecycle(store, clock):
    assert store.token(APPLE_USER_TOKEN) is None
    store.set_token(APPLE_USER_TOKEN, "MUT1")
    tok = store.token(APPLE_USER_TOKEN)
    assert (tok.value, tok.obtained_at, tok.invalid) == ("MUT1", clock.now, False)

    store.mark_token_invalid(APPLE_USER_TOKEN)
    assert store.token(APPLE_USER_TOKEN).invalid
    assert store.valid_token(APPLE_USER_TOKEN) is None

    clock.now += 10
    store.set_token(APPLE_USER_TOKEN, "MUT2")  # a new sign-in clears the flag
    assert store.valid_token(APPLE_USER_TOKEN) == "MUT2"
    assert store.token(APPLE_USER_TOKEN).obtained_at == clock.now


def test_link_opens_once_then_completes_once(store):
    raw = store.issue_link("apple")
    assert not store.complete_link("apple", raw)  # not opened yet
    assert store.open_link("apple", raw)
    assert not store.open_link("apple", raw)  # reuse refused
    assert store.complete_link("apple", raw)
    assert not store.complete_link("apple", raw)


def test_link_expires_after_15_minutes(store, clock):
    late_open = store.issue_link("apple")
    late_complete = store.issue_link("apple")
    assert store.open_link("apple", late_complete)
    clock.now += LINK_TTL_SECONDS
    assert not store.open_link("apple", late_open)
    assert not store.complete_link("apple", late_complete)


def test_link_bound_to_service_and_unguessable(store):
    raw = store.issue_link("apple")
    assert not store.open_link("spotify", raw)
    assert not store.open_link("apple", raw + "x")
    assert not store.open_link("apple", "")
    assert len(raw) >= 43  # 256 bits, url-safe


def test_link_stored_hashed(store, tmp_path):
    raw = store.issue_link("apple")
    dump = "\n".join(sqlite3.connect(tmp_path / "nested" / "state.db").iterdump())
    assert raw not in dump


def test_second_process_sees_writes(store, tmp_path, clock):
    """The `docker exec` CLI writes links into the file the server reads."""
    other = Store(tmp_path / "nested" / "state.db", clock=clock)
    raw = other.issue_link("apple")
    other.close()
    assert store.open_link("apple", raw)


def test_ownership_registry(store):
    assert not store.owns("p.1") and store.owned() == set()
    store.own("p.1", "created")
    store.own("p.1", "adopted")  # first record wins; no duplicate
    store.own("p.2", "adopted")
    assert store.owns("p.1") and store.owned() == {"p.1", "p.2"}
    store.disown("p.1")
    assert not store.owns("p.1") and store.owned() == {"p.2"}


def test_schema_version_set_and_reopen_is_a_no_op(tmp_path, clock):
    path = tmp_path / "state.db"
    s = Store(path, clock=clock)
    s.set_token(APPLE_USER_TOKEN, "MUT")
    s.close()
    again = Store(path, clock=clock)  # the migration is idempotent and keeps data
    assert again.valid_token(APPLE_USER_TOKEN) == "MUT"
    again.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 3


def test_newer_schema_refused(tmp_path, clock):
    path = tmp_path / "state.db"
    sqlite3.connect(path).execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema 99, newer"):
        Store(path, clock=clock)


def test_two_writers_share_the_file(tmp_path, clock):
    """The webhook handler and the loop each hold a connection in WAL mode."""
    path = tmp_path / "state.db"
    a, b = Store(path, clock=clock), Store(path, clock=clock)
    a.add_arrival("rock", "spotify:1", 200_000, "webhook")
    b.add_arrival("rock", "spotify:2", 200_000, "feed")
    assert [r["key"] for r in a.arrivals("rock", 0)] == ["spotify:1", "spotify:2"]
    a.close()
    b.close()


def test_errors_are_logged_once_per_failure(store, clock):
    store.record_sync("rock", "feed: down")
    clock.now += 20
    store.record_sync("rock", "feed: down")  # the same failure, retried
    clock.now += 20
    store.record_sync("rock", "api rail: HTTP 500")  # a different one
    clock.now += 20
    store.record_sync("rock")  # recovered
    clock.now += 20
    store.record_sync("rock", "feed: down")  # a new failure episode
    assert [r["detail"] for r in reversed(store.history("rock", 0))] == [
        "feed: down",
        "api rail: HTTP 500",
        "feed: down",
    ]
