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

## Credits

The web-player rail (`src/mcp_apple_music/apple_web.py`: amp-api endpoints and headers,
web-player token extraction, `canEdit` classification) and the rolling-window 429 handling
are ported from [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp),
MIT License, Copyright (c) 2024 Eric Pheterson, and rewritten on httpx.
