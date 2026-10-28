"""End-to-end check of both rails on a scratch playlist.

The opt-in live tests and the daily canary run this same routine: create a
playlist, append one song twice, remove one copy, delete the playlist. It records
how many copies the removal left, which is how the server learns whether Apple's
removal takes out one occurrence or all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .library import Library
from .rails import AppleError

PREFIX = "mcp-apple-music canary"
PROBE_TERM = "Daft Punk Get Lucky"  # any long-lived catalog song will do


@dataclass
class CanaryResult:
    ok: bool = False
    steps: list[str] = field(default_factory=list)
    copies_left: int | None = None  # of two, after removing one; None if not reached
    error: str | None = None


async def run(library: Library) -> CanaryResult:
    result = CanaryResult()
    log = result.steps.append
    playlist_id: str | None = None
    try:
        hits = await library.api.search_catalog(PROBE_TERM, limit=1)
        if not hits.data:
            raise RuntimeError(
                "probe song not found"
                + (" (possibly throttled)" if hits.possibly_throttled else "")
            )
        song = hits.data[0]["id"]
        log(f"probe song {song}")

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        created = await library.api.create_playlist(
            f"{PREFIX} {stamp}", "Temporary. The canary deletes it when it finishes."
        )
        playlist_id = created.id
        log(f"created {playlist_id} ({created.rail})")

        added = await library.add_tracks(playlist_id, [song, song])
        log(f"appended twice ({added.rail}): {added.reason or 'verified'}")
        if not added.ok:
            raise RuntimeError(f"append: {added.reason}")

        first = (await library.web.playlist_tracks(playlist_id)).data[0]["id"]
        removed = await library.remove_tracks(playlist_id, [first])
        log(f"removed one copy ({removed.rail}): {removed.reason or 'verified'}")
        if removed.tracks is not None:  # the playlist holds only the probe song
            result.copies_left = len(removed.tracks)
            log(f"copies left: {result.copies_left} of 2")
        if not removed.ok:
            raise RuntimeError(f"remove: {removed.reason}")
        result.ok = True
    except (AppleError, RuntimeError) as e:
        result.error = str(e)
        log(f"failed: {e}")
    finally:
        if playlist_id:
            try:
                deleted = await library.web.delete_playlist(playlist_id)
                log(f"deleted {playlist_id} ({deleted.rail})")
            except AppleError as e:
                result.ok = False
                result.error = result.error or f"cleanup: {e}"
                log(f"could not delete scratch playlist {playlist_id}: {e}")
    return result
