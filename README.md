# mcp-apple-music

MCP server for Apple Music: search the catalog and your library, manage playlists, and keep
"mirrored" playlists in sync with an external source (a JSON feed, a webhook, or a Spotify
playlist) as a rolling window.

Work in progress.

## Two rails

Everything Apple documents (search, ISRC lookup, library reads, creating playlists,
appending tracks) uses the official Apple Music API with your own developer token.
Removing tracks, renaming and deleting playlists use the web player's undocumented API,
and only for playlists this server created. Why, and what happens if Apple closes it:
[ADR 0001](docs/decisions/0001-web-player-rail.md).

## Tools

Two rules shape every tool:

- **Editing:** the server only changes playlists it created (or that a mirror adopted).
  `list_playlists` shows which ones are `editable`; for any other playlist, create a new one.
- **Two kinds of ID:** `add_tracks` takes catalog IDs (`catalog_id` from `search`,
  `lookup_isrc` or `get_playlist`); `remove_tracks` takes positions or track `id`s from
  `get_playlist`. Copies of a song in a playlist share one ID, and Apple removes the topmost.

Adds and removals succeed only once a re-read of the playlist shows them. They are not
idempotent: after an error, read the playlist before retrying.

| Tool | Example call | What comes back |
|---|---|---|
| `search` | `{"query": "Dopamine", "artist": "EYLR"}` | Songs where any credited artist matches, featured ones included: "Dopamine (feat. EYLR)". |
| `lookup_isrc` | `{"isrcs": ["usqx91300108", "USWB1-0400052", "GBAYE0000001", "abc"]}` | The matched songs in input order, `unmatched` ISRCs, and `invalid` inputs. |
| `list_playlists` | `{"name_contains": "road"}` | Library playlists with track counts and `editable`. |
| `get_playlist` | `{"playlist_id": "p.mine", "offset": 2, "limit": 2}` | Tracks with their `position`, plus `total` and `truncated`. |
| `create_playlist` | `{"name": "ROCK 24", "song_ids": ["100"]}` | The new playlist, filled and re-read in one call. |
| `rename_playlist` | `{"playlist_id": "p.new", "name": "ROCK"}` | The new name and `previous_name`. |
| `delete_playlist` | `{"playlist_id": "p.mine", "confirm": true}` | The deleted playlist's name. Without `confirm` it asks you to check with the user. |
| `add_tracks` | `{"playlist_id": "p.mine", "song_ids": ["100"], "isrcs": ["USQX91300108", "usqx91300108", "GBAYE0000001", "abc"]}` | Where each song landed; unmatched and invalid ISRCs listed. At most 25 per call. |
| `remove_tracks` | `{"playlist_id": "p.mine", "positions": [2]}` | What was removed from which position. A lower copy of a repeated song is refused. |
| `sign_in_link` | `{}` | A single-use, 15-minute link to the Apple Music sign-in page, for a person to open. |
| `mirror_status` | `{"name": "rock"}` | Window size, last sync and error, source coverage, tracks Apple lacks, ambiguous matches, recent repairs. |
| `sync_mirror` | `{"name": "rock"}` | Polls and syncs now; the tracks added, removed and failed. Needs `MIRRORS_ENABLED=true`. |

## Mirrors

A mirror keeps one Apple Music playlist equal to the last `window_seconds` of music from a
source, for example a radio station's last 24 hours. Mirrors are defined in a YAML file
(copy [`mirrors.example.yaml`](mirrors.example.yaml); a five-station setup is in
[`docs/examples/five-stations.yaml`](docs/examples/five-stations.yaml)):

```sh
MIRRORS_FILE=/data/mirrors.yaml
MIRRORS_ENABLED=true           # off by default: adopting writes to playlists people follow
WEBHOOK_SECRET=<random>        # enables POST /hooks/<secret>
SPOTIFY_CLIENT_ID=…            # optional: ISRC matching through a Spotify app
SPOTIFY_CLIENT_SECRET=…
```

**Sources:**
- **`feed`:** a JSON URL that returns one track or a list of them,
  `{"id", "name", "artists", "album", "duration_ms"}`. It's polled every `interval_seconds`
  (default 20), and whatever wasn't in the previous response is new. Redirects are
  followed on every poll.
- **`webhook`:** tracks are pushed to `POST /hooks/<secret>` as
  `{"playlist": "<webhook_key>", "id", "name", "artists", "album", "duration"}`, where a
  `duration` below 10,000 is read as seconds. Only `id`, or `name` plus `artists`, is
  required.
  - Every mirror accepts pushes under its `webhook_key` (default: its name), including
    feed mirrors. The same track arriving twice within an hour, by push and by feed,
    counts once.
- **`spotify`:** a Spotify playlist read with its owner's login. Coming next; such mirrors
  are skipped until then.

**Matching:** Spotify ID → ISRC → the Apple catalog. Without an ISRC match, a title search
is accepted only for the same title (version suffixes ignored) by an overlapping artist.
No other version is ever substituted. A track Apple lacks is left out, still counts toward
the window's length, and is listed by `mirror_status`.

**Keeping the playlist in step:**
- Each sync compares the playlist with the window. Expired tracks are removed from the
  top (Apple removes the topmost copy of a song), new ones are appended at the end, and
  the order is never changed.
- Anything else, such as a track added by hand or one lost to a failed write, is repaired
  and logged. A sync also runs every `reconcile_seconds` (default an hour) with nothing new.
- A drift above ten tracks is refused as a likely bad read.
- If removal stops working, appends pause once the playlist reaches 1.5 windows.

**Target playlist:** without `playlist_id`, the first run creates one (never matched by
name). With `playlist_id`, the mirror adopts that playlist and keeps its link. Its tracks
become the oldest part of the window, so they leave one by one as new tracks arrive.

**For listeners:** start a few tracks in. The top track is always the next to leave, and
when the playing track is removed the iPhone skips to the next one.

Events go to stderr as JSON lines (`{"mirror_event": "skipped", …}`) next to the usage
telemetry: `created`, `adopted`, `skipped`, `ambiguous_match`, `repair`, `drift_refused`,
`feed_failed`, `backoff`, `sync_failed`.

## Credits

The web-player rail (`src/mcp_apple_music/apple_web.py`: amp-api endpoints and headers,
web-player token extraction, `canEdit` classification) and the rolling-window 429 handling
are ported from [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp),
MIT License, Copyright (c) 2024 Eric Pheterson, and rewritten on httpx.
