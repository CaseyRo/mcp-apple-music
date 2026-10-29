"""Mirror definitions, read from a YAML file (see mirrors.example.yaml).

A mirror keeps one Apple Music playlist equal to a rolling window of tracks from
one source. Every mirror also accepts webhook pushes under its ``webhook_key``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedSource(_Strict):
    type: Literal["feed"]
    url: str = Field(pattern=r"^https?://")
    interval_seconds: int = Field(20, ge=5)


class WebhookSource(_Strict):
    """Tracks arrive only by push, to POST /hooks/<secret>."""

    type: Literal["webhook"]


class SpotifySource(_Strict):
    """A Spotify playlist read with its owner's login: the full desired list."""

    type: Literal["spotify"]
    playlist_id: str = Field(min_length=1)
    interval_seconds: int = Field(300, ge=60)


Source = Annotated[
    FeedSource | WebhookSource | SpotifySource, Field(discriminator="type")
]


class Mirror(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    source: Source
    playlist_name: str = Field(min_length=1)
    # Adopt this existing playlist; without it the first run creates one. Never by name.
    playlist_id: str | None = None
    window_seconds: int = Field(86_400, ge=60)
    webhook_key: str | None = None  # the payload's `playlist` value; defaults to name
    reconcile_seconds: int = Field(3600, ge=60)

    @property
    def key(self) -> str:
        return (self.webhook_key or self.name).casefold()


def parse(data: object) -> list[Mirror]:
    """Validate every entry; the error names the entry that failed."""
    entries = data.get("mirrors") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ConfigError("the file needs a top-level `mirrors:` list")
    mirrors: list[Mirror] = []
    for i, raw in enumerate(entries, 1):
        label = raw.get("name") if isinstance(raw, dict) else None
        label = f"mirror {i} ({label})" if label else f"mirror {i}"
        try:
            mirrors.append(Mirror.model_validate(raw))
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'entry'}: {err['msg']}"
                for err in e.errors()
            )
            raise ConfigError(f"{label}: {problems}") from None
    for what, values in (
        ("name", [m.name for m in mirrors]),
        ("webhook key", [m.key for m in mirrors]),
    ):
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise ConfigError(f"duplicate mirror {what}: {', '.join(dupes)}")
    return mirrors


def load(path: Path) -> list[Mirror]:
    try:
        return parse(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from None
