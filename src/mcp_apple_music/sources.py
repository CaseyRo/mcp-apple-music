"""Sources turn what a station reports into Tracks (design D8).

Both known payload shapes go through one normalizer:
- feed: ``{id, name, artists, album, duration_ms}``;
- webhook: ``{playlist, id, name, artists, album, duration}``, where ``duration``
  below 10,000 is seconds and otherwise milliseconds.
"""

from __future__ import annotations

import re

import httpx

from .mirror import Track
from .mirror_config import FeedSource
from .store import Store

_SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")
SECONDS_BELOW = 10_000


class SourceError(Exception):
    pass


def _text(value, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, list):  # a list of names instead of the comma-joined form
        value = ", ".join(str(v) for v in value)
    if not isinstance(value, str | int | float):
        raise ValueError(f"`{field}` must be text")
    return str(value).strip()[:500]


def track_from_payload(obj: object) -> Track:
    """Raises ValueError when the payload is not a usable track."""
    if not isinstance(obj, dict):
        raise ValueError("a track must be a JSON object")
    sid = _text(obj.get("id"), "id") or None
    if sid and not _SPOTIFY_ID.match(sid):
        raise ValueError("`id` is not a Spotify track ID")
    title, artists = (
        _text(obj.get("name"), "name"),
        _text(obj.get("artists"), "artists"),
    )
    if not sid and not (title and artists):
        raise ValueError("a track needs an `id`, or a `name` and `artists`")
    raw = obj.get("duration_ms", obj.get("duration"))
    duration = None
    if raw not in (None, ""):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError("`duration` must be a number") from None
        if value > 0:
            duration = int(value * 1000 if value < SECONDS_BELOW else value)
    album = _text(obj.get("album"), "album") or None
    return Track(title, artists, album, sid, duration)


class Feed:
    """A polled JSON feed. What wasn't in the previous response is new."""

    def __init__(self, store: Store, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self._http = httpx.AsyncClient(
            transport=transport, timeout=10, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def poll(self, mirror: str, source: FeedSource) -> list[Track]:
        """New tracks in response order. On any failure the cursor is kept, so the
        next poll compares against the last good response."""
        try:
            resp = await self._http.get(source.url)  # redirects followed every time
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SourceError(f"feed {source.url}: {e}") from e
        items = body if isinstance(body, list) else [body]
        tracks = []
        for item in items:
            try:
                tracks.append(track_from_payload(item))
            except ValueError:
                continue  # one malformed item doesn't hide the others
        previous = set(self.store.feed_cursor(mirror) or [])
        self.store.set_feed_cursor(mirror, [t.key for t in tracks])
        return [t for t in tracks if t.key not in previous]
