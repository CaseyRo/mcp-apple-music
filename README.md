# mcp-apple-music

MCP server for Apple Music: search the catalog and your library, manage playlists, and keep
"mirrored" playlists in sync with an external source (a JSON feed, a webhook, or a Spotify
playlist) as a rolling window.

Work in progress.

## Credits

The web-player rail (`src/mcp_apple_music/apple_web.py`: amp-api endpoints and headers,
web-player token extraction, `canEdit` classification) and the rolling-window 429 handling
are ported from [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp),
MIT License, Copyright (c) 2024 Eric Pheterson, and rewritten on httpx.
