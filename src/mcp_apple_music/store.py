"""All mutable state, in one SQLite file in WAL mode under the data dir.

The webhook handler, the mirror loop and a `docker exec` CLI may write at the same
time; WAL lets them without an extra service.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

APPLE_USER_TOKEN = "apple_music_user"
LINK_TTL_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    obtained_at REAL NOT NULL,
    invalid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS owned_playlists (
    playlist_id TEXT PRIMARY KEY,
    how TEXT NOT NULL,
    since REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sign_in_links (
    hash TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    created_at REAL NOT NULL,
    opened_at REAL,
    used_at REAL
);
"""


@dataclass
class Token:
    value: str
    obtained_at: float
    invalid: bool


def _hash(raw: str) -> str:
    # The link token is 256 random bits, so a plain digest is enough to store it.
    return hashlib.sha256(raw.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._clock = clock

    def close(self) -> None:
        self._db.close()

    # -- tokens --------------------------------------------------------------

    def token(self, name: str) -> Token | None:
        row = self._db.execute(
            "SELECT value, obtained_at, invalid FROM tokens WHERE name = ?", (name,)
        ).fetchone()
        return Token(row[0], row[1], bool(row[2])) if row else None

    def valid_token(self, name: str) -> str | None:
        tok = self.token(name)
        return tok.value if tok and not tok.invalid else None

    def set_token(self, name: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO tokens (name, value, obtained_at, invalid)"
            " VALUES (?, ?, ?, 0)",
            (name, value, self._clock()),
        )

    def mark_token_invalid(self, name: str) -> None:
        self._db.execute("UPDATE tokens SET invalid = 1 WHERE name = ?", (name,))

    # -- playlists this server may edit (design D12) --------------------------
    # Apple marks nearly every user playlist editable, so its flag can't be the
    # gate: a playlist is ours when we created it or a mirror adopted it by ID.

    def own(self, playlist_id: str, how: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO owned_playlists (playlist_id, how, since)"
            " VALUES (?, ?, ?)",
            (playlist_id, how, self._clock()),
        )

    def disown(self, playlist_id: str) -> None:
        self._db.execute(
            "DELETE FROM owned_playlists WHERE playlist_id = ?", (playlist_id,)
        )

    def owns(self, playlist_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM owned_playlists WHERE playlist_id = ?", (playlist_id,)
            ).fetchone()
            is not None
        )

    def owned(self) -> set[str]:
        return {
            r[0] for r in self._db.execute("SELECT playlist_id FROM owned_playlists")
        }

    # -- single-use sign-in links -------------------------------------------
    # A link is opened once (GET serves the page) and then completed once (the
    # page's POST). Both steps must happen within LINK_TTL_SECONDS of issue.

    def issue_link(self, service: str) -> str:
        raw = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO sign_in_links (hash, service, created_at) VALUES (?, ?, ?)",
            (_hash(raw), service, self._clock()),
        )
        return raw

    def open_link(self, service: str, raw: str) -> bool:
        return self._advance(service, raw, "opened_at", "opened_at IS NULL")

    def complete_link(self, service: str, raw: str) -> bool:
        return self._advance(
            service, raw, "used_at", "opened_at IS NOT NULL AND used_at IS NULL"
        )

    def _advance(self, service: str, raw: str, column: str, state: str) -> bool:
        now = self._clock()
        cur = self._db.execute(
            f"UPDATE sign_in_links SET {column} = ? WHERE hash = ? AND service = ?"
            f" AND created_at > ? AND {state}",
            (now, _hash(raw), service, now - LINK_TTL_SECONDS),
        )
        return cur.rowcount == 1
