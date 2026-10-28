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

## Credits

The web-player rail (`src/mcp_apple_music/apple_web.py`: amp-api endpoints and headers,
web-player token extraction, `canEdit` classification) and the rolling-window 429 handling
are ported from [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp),
MIT License, Copyright (c) 2024 Eric Pheterson, and rewritten on httpx.
