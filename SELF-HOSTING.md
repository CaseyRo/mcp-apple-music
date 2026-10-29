# Self-hosting

Run your own mcp-apple-music with Docker. It needs about 15 minutes, most of it in
Apple's developer portal.

## What you need

- An **Apple Developer Program** membership (paid, yearly). Apple issues the MusicKit
  key that lets the server use the Apple Music API only to members.
- An **Apple Music** subscription on the Apple ID whose library the server manages.
- **Docker** with Compose.
- Optional: a free **Spotify developer app**. Without it, mirrors match tracks by
  title and artist; with it, they match exactly, by ISRC.

## 1. Create the MusicKit key

In the [Apple Developer portal](https://developer.apple.com/account), under
**Certificates, Identifiers & Profiles**:

1. **Identifiers** → **+** → **Media IDs** → Continue. Give it a description (for
   example "MCP Apple Music") and an identifier such as `media.com.example.mcp-apple-music`.
   Tick **MusicKit**, then Continue and Register.
2. **Keys** → **+**. Name the key and tick **Media Services (MusicKit, ShazamKit, Apple
   Music Feed)**. Click **Configure**, choose the Media ID from step 1, Save, Continue
   and Register.
3. **Download** the `.p8` file. Apple lets you download it **once**. Note the **Key ID**
   on that page.
4. Your **Team ID** is under **Membership details**.

Keep the `.p8` somewhere safe, such as a password manager, rather than in a synced
folder. The server only needs its contents.

## 2. Optional: create a Spotify app

At the [Spotify developer dashboard](https://developer.spotify.com/dashboard):

1. **Create app**. Give it any name and description.
2. **Redirect URI:** `http://127.0.0.1:8000/auth/spotify/callback`. Spotify accepts
   plain `http` only for a loopback IP written as `127.0.0.1`, not `localhost`. Behind
   a public HTTPS address, add `https://<your host>/auth/spotify/callback` as well.
   The current version doesn't use the redirect yet: it's for the coming Spotify-playlist
   source, which needs the playlist owner's sign-in.
3. Under **Which API/SDKs are you planning to use?**, tick **Web API**, then Save.
4. Open **Settings** and copy the **Client ID** and **Client secret**.

## 3. Start the server

```sh
mkdir mcp-apple-music && cd mcp-apple-music
curl -fsSLO https://raw.githubusercontent.com/CaseyRo/mcp-apple-music/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/CaseyRo/mcp-apple-music/main/.env.example -o .env
```

Fill in `.env`:

- `MCP_API_KEY`: a new random key (`openssl rand -hex 32`). Your MCP client sends it.
- `APPLE_TEAM_ID`, `APPLE_KEY_ID`, and `APPLE_PRIVATE_KEY`, which takes the `.p8`
  contents. Paste it on one line with `\n` escapes, or just the base64 body between the
  `BEGIN` and `END` lines.
- `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, if you made the app.

Then:

```sh
docker compose up -d
curl -s http://127.0.0.1:8000/health
```

`/health` answers `"status": "degraded"` with the problem `Apple Music sign-in
needed`. That's expected: the next step fixes it.

The server binds `127.0.0.1` only, so it's reachable from this machine and nowhere
else.

## 4. Sign in to Apple Music

```sh
docker compose exec mcp-apple-music mcp-apple-music sign-in-link apple
```

Open the printed link in your browser within 15 minutes and sign in with the Apple ID
that has Apple Music. The page confirms with `Signed in (storefront xx)`. Each link
works once.

An agent can also get a link with the `sign_in_link` tool.

`/health` now shows `apple_user_token` as `valid` and the status as `healthy`. Apple
doesn't say how long this sign-in lasts: reports range from days to six months. When
`/health` says `expiring soon` or `invalid`, sign in the same way again.

## 5. Connect an MCP client

The MCP endpoint is `http://127.0.0.1:8000/mcp`, using the streamable HTTP transport.
Send `Authorization: Bearer <MCP_API_KEY>`. With Claude Code:

```sh
claude mcp add --transport http apple-music http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $MCP_API_KEY"
```

## 6. Mirrors

A mirror keeps a playlist equal to the last hours of music from a source. The README's
[Mirrors section](README.md#mirrors) explains sources, matching and adoption.

1. Copy [`mirrors.example.yaml`](mirrors.example.yaml) to `./config/mirrors.yaml` and
   edit it. It holds no secrets.
2. In `.env`, set `MIRRORS_FILE=/config/mirrors.yaml` and `MIRRORS_ENABLED=true`.
3. Run `docker compose up -d`. The first sync creates each playlist, or adopts the one
   named by `playlist_id`. Then ask your agent for `mirror_status`.

Mirrors stay off until `MIRRORS_ENABLED=true`, because adopting writes to playlists
people may follow.

## 7. Webhook, for a feed owner who pushes tracks

Set `WEBHOOK_SECRET` (`openssl rand -hex 24`) and restart. The secret is part of the
URL, so whoever pushes needs no headers beyond the content type:

```sh
curl -X POST "https://music.example.com/hooks/$WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"playlist": "rock", "id": "4uLU6hMCjMI75M1A2tKUQC",
       "name": "Dreams", "artists": "Fleetwood Mac", "album": "Rumours", "duration": 257}'
```

The payload:
- `playlist` is the mirror's `webhook_key`, which defaults to its name.
- `id` is the Spotify track ID.
- `duration` is in seconds below 10,000, otherwise in milliseconds.

Only `id`, or `name` plus `artists`, is required.

| Answer | Meaning |
|---|---|
| `202` | Stored; the mirror syncs within seconds |
| `400` | Not a usable track |
| `404` | Wrong secret, or no `WEBHOOK_SECRET` set |
| `413` | Body over 16 KB |
| `422` | No mirror has that `playlist` key |

For pushes from elsewhere, the server has to be reachable from outside. Put a tunnel
or reverse proxy in front of it and set `PUBLIC_URL` to that address (sign-in links
use it). Without the API key, only `/health`, `/hooks/<secret>` and `/auth/*` answer;
every other path returns 401.

## Updating and backups

- **Update:** `docker compose pull && docker compose up -d`. Releases are git tags; the
  image's `/health` reports its version.
- **Backups:** everything that matters is in the `apple-music-data` volume: sign-ins,
  mirror windows, playlist IDs and the reconcile history. The MusicKit key and API keys
  live in your `.env`.

## Limits

- One Apple account per instance.
- Removing tracks, renaming and deleting playlists use the Apple Music web player's
  undocumented API. If Apple changes it, those stop and everything else keeps working;
  see [ADR 0001](docs/decisions/0001-web-player-rail.md). A daily canary checks it,
  and `/health` reports the result.
