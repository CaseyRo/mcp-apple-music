"""All mutable state, in one SQLite file in WAL mode under the data dir.

The webhook handler, the mirror loop and a `docker exec` CLI may write at the same
time; WAL lets them without an extra service.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

APPLE_USER_TOKEN = "apple_music_user"
LINK_TTL_SECONDS = 15 * 60
SCHEMA_VERSION = 2  # 1: tokens, owned playlists, links; 2: mirror tables

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
CREATE TABLE IF NOT EXISTS mirrors (
    name TEXT PRIMARY KEY,
    playlist_id TEXT,
    created_at REAL NOT NULL,
    last_sync REAL,
    last_error TEXT,
    last_error_at REAL
);
-- One row per occurrence (design D5), in seq order. Rows that left the window
-- stay, marked expired, until their copy is gone from the playlist.
CREATE TABLE IF NOT EXISTS window_entries (
    mirror TEXT NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT,
    spotify_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    artists TEXT NOT NULL DEFAULT '',
    album TEXT,
    duration_ms INTEGER,
    arrived_at REAL NOT NULL,
    apple_song_id TEXT,
    match_method TEXT,
    unavailable_reason TEXT,
    isrc TEXT,
    placed INTEGER NOT NULL DEFAULT 0,
    expired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mirror, seq)
);
CREATE TABLE IF NOT EXISTS arrivals (
    mirror TEXT NOT NULL,
    key TEXT NOT NULL,
    text_key TEXT,
    at REAL NOT NULL,
    duration_ms INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS arrivals_by_mirror ON arrivals (mirror, at);
CREATE TABLE IF NOT EXISTS repairs (
    mirror TEXT NOT NULL,
    at REAL NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_cursor (
    mirror TEXT PRIMARY KEY,
    ids TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    name TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    at REAL NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    key TEXT PRIMARY KEY,
    apple_song_id TEXT,
    method TEXT,
    isrc TEXT,
    duration_ms INTEGER,
    reason TEXT,
    at REAL NOT NULL
);
"""

ENTRY_FIELDS = (
    "key",
    "spotify_id",
    "title",
    "artists",
    "album",
    "duration_ms",
    "arrived_at",
    "apple_song_id",
    "match_method",
    "unavailable_reason",
    "isrc",
    "placed",
    "expired",
)


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
        self._db.row_factory = sqlite3.Row
        found = self._db.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} has schema {found}, newer than this server's {SCHEMA_VERSION}:"
                " run a newer mcp-apple-music, or restore a backup of the data dir."
            )
        self._db.executescript(
            _SCHEMA
        )  # every table is IF NOT EXISTS: adds, never alters
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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

    # -- mirrors (design D4, D5) -----------------------------------------------

    def mirror(self, name: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM mirrors WHERE name = ?", (name,)
        ).fetchone()

    def bind_mirror(self, name: str, playlist_id: str) -> None:
        """Point a mirror at a playlist. Switching playlists empties the window."""
        row = self.mirror(name)
        if row and row["playlist_id"] == playlist_id:
            return
        if row and row["playlist_id"]:
            self._db.execute("DELETE FROM window_entries WHERE mirror = ?", (name,))
        self._db.execute(
            "INSERT INTO mirrors (name, playlist_id, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET playlist_id = excluded.playlist_id",
            (name, playlist_id, self._clock()),
        )

    def record_sync(self, name: str, error: str | None = None) -> None:
        """A sync's outcome; kept even for a mirror whose playlist isn't set up yet."""
        now = self._clock()
        self._db.execute(
            "INSERT INTO mirrors (name, created_at) VALUES (?, ?)"
            " ON CONFLICT (name) DO NOTHING",
            (name, now),
        )
        if error:
            self._db.execute(
                "UPDATE mirrors SET last_error = ?, last_error_at = ? WHERE name = ?",
                (error, now, name),
            )
        else:
            self._db.execute(
                "UPDATE mirrors SET last_sync = ? WHERE name = ?", (now, name)
            )

    def arrived_within(self, mirror: str, keys: list[str], seconds: float) -> bool:
        """Whether any of a track's keys (Spotify ID, title|artists) arrived lately."""
        marks = ", ".join("?" * len(keys))
        return (
            self._db.execute(
                f"SELECT 1 FROM arrivals WHERE mirror = ? AND at > ?"
                f" AND (key IN ({marks}) OR text_key IN ({marks}))",
                (mirror, self._clock() - seconds, *keys, *keys),
            ).fetchone()
            is not None
        )

    def add_arrival(
        self,
        mirror: str,
        key: str,
        duration_ms: int | None,
        source: str,
        text_key: str | None = None,
    ) -> None:
        now = self._clock()
        self._db.execute(
            "INSERT INTO arrivals (mirror, key, text_key, at, duration_ms, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (mirror, key, text_key, now, duration_ms, source),
        )
        # Two days covers the dedupe horizon and the 24 h coverage figure.
        self._db.execute("DELETE FROM arrivals WHERE at < ?", (now - 2 * 86_400,))

    def arrivals(self, mirror: str, since: float) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM arrivals WHERE mirror = ? AND at > ? ORDER BY at",
            (mirror, since),
        ).fetchall()

    def add_entries(
        self, mirror: str, rows: list[dict], *, before: bool = False
    ) -> None:
        """Append window entries, or with ``before`` put them ahead of all existing
        ones (an adopted playlist's tracks are older than anything that arrived)."""
        lo, hi = self._db.execute(
            "SELECT COALESCE(MIN(seq), 1), COALESCE(MAX(seq), 0)"
            " FROM window_entries WHERE mirror = ?",
            (mirror,),
        ).fetchone()
        start = lo - len(rows) if before else hi + 1
        now = self._clock()
        for seq, row in enumerate(rows, start):
            row = {"arrived_at": now, **row}
            cols = ", ".join(row)
            self._db.execute(
                f"INSERT INTO window_entries (mirror, seq, {cols})"
                f" VALUES (?, ?, {', '.join('?' * len(row))})",
                (mirror, seq, *row.values()),
            )

    def entries(self, mirror: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM window_entries WHERE mirror = ? ORDER BY seq", (mirror,)
        ).fetchall()

    def update_entry(self, mirror: str, seq: int, **fields) -> None:
        assert fields and set(fields) <= set(ENTRY_FIELDS), fields
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE window_entries SET {sets} WHERE mirror = ? AND seq = ?",
            (*fields.values(), mirror, seq),
        )

    def delete_entries(self, mirror: str, seqs: list[int]) -> None:
        self._db.executemany(
            "DELETE FROM window_entries WHERE mirror = ? AND seq = ?",
            [(mirror, s) for s in seqs],
        )

    def add_repair(self, mirror: str, action: str, detail: str) -> None:
        self._db.execute(
            "INSERT INTO repairs (mirror, at, action, detail) VALUES (?, ?, ?, ?)",
            (mirror, self._clock(), action, detail),
        )

    def repairs(self, mirror: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM repairs WHERE mirror = ? ORDER BY at DESC, rowid DESC"
            " LIMIT ?",
            (mirror, limit),
        ).fetchall()

    # -- periodic checks (the canary) -------------------------------------------

    def set_check(self, name: str, ok: bool, detail: str | None = None) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO checks (name, ok, at, detail) VALUES (?, ?, ?, ?)",
            (name, int(ok), self._clock(), detail),
        )

    def check(self, name: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM checks WHERE name = ?", (name,)
        ).fetchone()

    # -- match cache and feed cursors -------------------------------------------

    def cached_match(self, key: str, miss_ttl: float) -> sqlite3.Row | None:
        """A cached match; a miss only while younger than ``miss_ttl`` (Apple adds
        songs, so a miss is worth asking again later)."""
        return self._db.execute(
            "SELECT * FROM matches WHERE key = ?"
            " AND (apple_song_id IS NOT NULL OR at > ?)",
            (key, self._clock() - miss_ttl),
        ).fetchone()

    def put_match(self, key: str, **fields) -> None:
        cols = ["key", "at", *fields]
        self._db.execute(
            f"INSERT OR REPLACE INTO matches ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})",
            (key, self._clock(), *fields.values()),
        )

    def feed_cursor(self, mirror: str) -> list[str] | None:
        row = self._db.execute(
            "SELECT ids FROM feed_cursor WHERE mirror = ?", (mirror,)
        ).fetchone()
        return json.loads(row["ids"]) if row else None

    def set_feed_cursor(self, mirror: str, ids: list[str]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO feed_cursor (mirror, ids, at) VALUES (?, ?, ?)",
            (mirror, json.dumps(ids), self._clock()),
        )
