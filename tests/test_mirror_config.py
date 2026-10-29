"""Mirror definitions: one file, validated entry by entry."""

from __future__ import annotations

import pytest

from mcp_apple_music import mirror_config
from mcp_apple_music.mirror_config import ConfigError

FEED = {"type": "feed", "url": "https://radio.example/api/rock.json"}


def mirror(name: str, **kw) -> dict:
    return {"name": name, "playlist_name": f"Station {name}", "source": FEED, **kw}


def test_five_feed_mirrors():
    names = ["rock", "fresh", "relax", "soul", "music"]
    mirrors = mirror_config.parse({"mirrors": [mirror(n) for n in names]})
    assert [m.name for m in mirrors] == names
    rock = mirrors[0]
    assert (rock.window_seconds, rock.source.interval_seconds, rock.key) == (
        86_400,
        20,
        "rock",
    )


def test_entry_missing_its_source_is_named():
    entries = [mirror("rock"), {"name": "soul", "playlist_name": "Station Soul"}]
    with pytest.raises(ConfigError, match=r"mirror 2 \(soul\): source: Field required"):
        mirror_config.parse({"mirrors": entries})


@pytest.mark.parametrize(
    "entries, message",
    [
        ([mirror("rock"), mirror("rock")], "duplicate mirror name: rock"),
        (
            [mirror("rock"), mirror("hard", webhook_key="ROCK")],
            "duplicate mirror webhook key: rock",
        ),
        ([mirror("rock", sorce=FEED)], r"mirror 1 \(rock\): sorce: Extra inputs"),
        ([mirror("rock", source={"type": "radio"})], r"mirror 1 \(rock\): source"),
        ([mirror("Rock Station")], r"mirror 1 \(Rock Station\): name"),
    ],
)
def test_invalid_files(entries, message):
    with pytest.raises(ConfigError, match=message):
        mirror_config.parse({"mirrors": entries})


def test_not_a_mirror_list():
    with pytest.raises(ConfigError, match="top-level `mirrors:` list"):
        mirror_config.parse({"mirror": []})


def test_example_files_are_valid():
    from pathlib import Path

    root = Path(__file__).parent.parent
    for path in [root / "mirrors.example.yaml", *root.glob("docs/examples/*.yaml")]:
        assert mirror_config.load(path), path


def test_inline_config_on_one_line(monkeypatch, tmp_path):
    """MIRRORS_CONFIG carries the same YAML in flow style; a file wins over it."""
    import mcp_apple_music.server as srv
    from mcp_apple_music.config import settings

    line = "{mirrors: [{name: rock, playlist_name: Rock, source: {type: webhook}}]}"
    monkeypatch.setattr(settings, "mirrors_config", line)
    assert [m.name for m in srv.mirrors()] == ["rock"]
    (tmp_path / "m.yaml").write_text(
        "mirrors: [{name: soul, playlist_name: S, source: {type: webhook}}]"
    )
    monkeypatch.setattr(settings, "mirrors_file", tmp_path / "m.yaml")
    srv.mirrors.cache_clear()
    assert [m.name for m in srv.mirrors()] == ["soul"]
    with pytest.raises(ConfigError, match="MIRRORS_CONFIG"):
        mirror_config.loads("{mirrors: [")
