# 0001. Use the web-player API only for track removal, rename and delete
Status: Accepted
Date: 2026-10-28
Amended: 2026-10-29: the web rail also sets playlist descriptions; "playlists this server created" became "created or adopted by a mirror", recorded by the server (design D12), because `canEdit` turned out to be true on nearly every user playlist.

## Context
A rolling mirrored playlist has to drop its oldest tracks as new ones arrive. Apple's documented Music API lets an app create library playlists and append tracks, and nothing more: there is no removal, rename or delete, and WWDC25 and WWDC26 left that unchanged. MusicKit's `MusicLibrary.edit` can edit, but only from an iOS-family app, which cannot run unattended in a Linux container. Scripting Music.app needs a Mac that stays awake, which is where an earlier setup of this kind broke down.

The web player at music.apple.com makes these edits through `amp-api.music.apple.com`, using a token embedded in its public JavaScript bundle plus the signed-in user's Music User Token. Other clients use the same path, among them Cider, Music Assistant and epheterson/applemusic-mcp, whose endpoint knowledge this repo ports under its MIT license. The path is undocumented, so Apple can change or close it without notice. It also falls outside the Apple Developer license's permission to use the services "as permitted in the Documentation".

## Decision
The server calls amp-api only to remove playlist tracks, rename, describe and delete playlists, and list the track IDs and counts that removal needs. Everything else goes through the documented API with the operator's own developer token, and only playlists this server created or a mirror adopted are ever edited.

## Consequences
- One module, `src/mcp_apple_music/apple_web.py`, owns every amp-api call, and every result names the rail that carried it, so a breakage stays contained and visible.
- If the web-player token cannot be obtained, removal, rename and delete report the rail as unavailable while search, create and append keep working. A mirror then grows past its window instead of failing.
- A daily canary (create, append, remove and delete on a scratch playlist) reports through `/health`, so monitoring sees a break within a day, before anyone notices a stale playlist.
- Fallback if Apple closes the path: a mirror pauses appends once it passes 1.5 times its window length, so it goes stale rather than ballooning.
- Each operator decides whether to accept the license position. The web rail is only ever called to edit playlists this server created, for an agent or for a configured mirror.
- Reconsider if Apple documents playlist editing in the Music API, or if amp-api rejects the harvested token for good.
