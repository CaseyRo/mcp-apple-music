# Prior art and what we took from it

Other projects reach Apple Music too, each in its own way. This server borrows from
several of them and differs on purpose where its job is different: running unattended in a
container, for listeners who follow its playlists.

## epheterson/applemusic-mcp

[github.com/epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp), an MCP
server for playlists, library, catalog, playback and the queue, on macOS, Windows and Linux.
MIT License, Copyright (c) 2024 Eric Pheterson.

**Taken, rewritten on httpx, with credit in the code:**
- The web player's API (`amp-api.music.apple.com`): the endpoints for removing playlist
  tracks, renaming and deleting playlists, and the `Origin: https://music.apple.com` header
  they need.
- Reading the web player's token out of its public JavaScript bundle.
- Classifying playlists as API-made, user-made or Apple-curated from `canEdit`.
- The observation that the web token's 429s last up to an hour.

**Done differently:**
- **Removal uses `mode=first`, not `mode=all`.** Tested on 2026-10-28: `mode` is required,
  and `first` removes exactly the topmost copy of a song while `all` removes every copy. A
  rolling window repeats songs, so it needs `first`.
- **`canEdit` is not the editing gate.** Apple sets it on 101 of 112 playlists in one
  library, including ones made in Music.app. This server keeps its own record of the
  playlists it created or took over, and edits only those.
- **No dependency on the package.** It pins an older MCP library, keeps tokens in the OS
  keychain and pulls in Playwright; this server runs in a slim Linux container.

## kennethreitz/mcp-applemusic

[github.com/kennethreitz/mcp-applemusic](https://github.com/kennethreitz/mcp-applemusic), an
MCP server that controls the Music app on macOS through AppleScript: playback and the local
library.

**Taken:** nothing in code. It's the clearest example of the AppleScript approach, and of
why this server doesn't use it: scripting Music.app needs a Mac that stays awake and signed
in. That is exactly what broke the earlier version of the radio mirrors this server now
runs.

## Music Assistant: the Apple Music provider

[music-assistant/server](https://github.com/music-assistant/server), `providers/apple_music`.
It brings Apple Music into a self-hosted music server.

**Taken:**
- **Signing in through a MusicKit JS page** served by the server itself, which hands the
  Music User Token back to it. This server does the same, behind single-use 15-minute
  links.
- **The page's referrer policy.** Music Assistant's sign-in page sends
  `strict-origin-when-cross-origin`. Ours first sent `no-referrer`, and MusicKit's
  `authorize()` failed with `AUTHORIZATION_ERROR: Unauthorized` until it sent the origin.

**Done differently:**
- **Credentials.** Music Assistant ships one developer token for all its users, so they
  need no developer account. This server signs its own token with the operator's MusicKit
  key, so self-hosters need an Apple Developer Program membership. A keyless mode in Music
  Assistant's style was considered and parked.
- **Removal.** Music Assistant's provider says removing playlist tracks is not supported by
  the Apple Music API. That's true of the documented API, but the web player's API does it.
  Our findings (`mode=first`, shared IDs between copies, track counts, rename and delete) are
  written up to share with Music Assistant once they have run in production for a while.
- **Rate limits.** Music Assistant reports 429s on the developer token clearing within a
  second. This server backs off for ten minutes, which may be more cautious than the
  documented rail needs.

## Cider

[cider.sh](https://cider.sh), a third-party Apple Music client. It also works through the web
player's API. Nothing is taken from it; it's evidence that this path is used by more than
one client.

## Apple's own options, and why they didn't fit

- **The Apple Music API** (documented) can create playlists and append, but has no removal,
  rename or delete. WWDC25 and WWDC26 left that unchanged.
- **MusicKit's `MusicLibrary.edit`** can edit playlists, but only from an iPhone, iPad or
  Apple TV app. That can't run unattended in a container, and iOS limits background work.
- **Shortcuts and Music.app scripting** both need a Mac in the loop.
