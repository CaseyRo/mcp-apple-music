"""What the tools return, and how Apple's JSON resources become it.

Apple credits featured artists in the title ("Dopamine (feat. EYLR)") and gives
one credit line for the rest ("Daft Punk, Pharrell Williams & Nile Rodgers", but
also "Simon & Garfunkel"), so a song carries the credit line verbatim plus the
featured names parsed from its title, and filters match whole words across both.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field

_FEATURED = re.compile(
    r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)[)\]]", re.IGNORECASE
)
_LIST = re.compile(r"\s*,\s*|\s+&\s+")


class Song(BaseModel):
    id: str = Field(
        description="Catalog results: the catalog ID. Library and playlist tracks: the "
        "library ID (i.…), which remove_tracks takes and add_tracks does not."
    )
    catalog_id: str | None = Field(
        description="The ID add_tracks takes. None for uploads not in the catalog."
    )
    title: str
    artist: str = Field(description="Apple's credit line, verbatim.")
    featured: list[str] = Field(
        default=[], description='Featured artists, from "(feat. …)" in the title.'
    )
    album: str | None = None
    duration_seconds: int | None = None
    isrc: str | None = Field(
        default=None, description="Catalog songs only; Apple gives library songs none."
    )


class Album(BaseModel):
    id: str
    title: str
    artist: str
    track_count: int | None = None
    release_date: str | None = None


class Artist(BaseModel):
    id: str
    name: str


class PlaylistRef(BaseModel):
    id: str
    name: str
    curator: str | None = None


def featured(title: str) -> list[str]:
    return [
        name
        for m in _FEATURED.finditer(title)
        for name in _LIST.split(m.group(1).strip())
        if name
    ]


def song(resource: dict) -> Song:
    a = resource.get("attributes", {})
    pp = a.get("playParams") or {}
    library = resource.get("type", "").startswith("library") or pp.get("isLibrary")
    ms = a.get("durationInMillis")
    return Song(
        id=resource["id"],
        catalog_id=pp.get("catalogId") if library else resource["id"],
        title=a.get("name", ""),
        artist=a.get("artistName", ""),
        featured=featured(a.get("name", "")),
        album=a.get("albumName"),
        duration_seconds=round(ms / 1000) if ms else None,
        isrc=a.get("isrc"),
    )


def album(resource: dict) -> Album:
    a = resource.get("attributes", {})
    return Album(
        id=resource["id"],
        title=a.get("name", ""),
        artist=a.get("artistName", ""),
        track_count=a.get("trackCount"),
        release_date=a.get("releaseDate"),
    )


def artist(resource: dict) -> Artist:
    return Artist(
        id=resource["id"], name=resource.get("attributes", {}).get("name", "")
    )


def playlist_ref(resource: dict) -> PlaylistRef:
    a = resource.get("attributes", {})
    return PlaylistRef(
        id=resource["id"], name=a.get("name", ""), curator=a.get("curatorName")
    )


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def matches(needle: str, *haystacks: str | None) -> bool:
    """Whole-word, case- and accent-insensitive: "EYLR" finds "(feat. EYLR)",
    "Eve" does not find "Steve"."""
    pattern = r"(?<!\w)" + re.escape(_fold(needle.strip())) + r"(?!\w)"
    return any(h and re.search(pattern, _fold(h)) for h in haystacks)


def song_matches(s: Song, artist: str | None, album: str | None) -> bool:
    return (not artist or matches(artist, s.artist, *s.featured)) and (
        not album or matches(album, s.album)
    )
