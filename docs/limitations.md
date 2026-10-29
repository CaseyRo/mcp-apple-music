# Limitations

What this server can't do, or can only do approximately, and what happens when it hits
those limits. Most of them come from Apple Music itself.

## Apple Music's side

- **Removing tracks depends on an undocumented API.** Apple's documented Music API can't
  remove, rename or delete, so those go through the web player's API
  ([ADR 0001](decisions/0001-web-player-rail.md)). Apple can change or close it without
  notice, and it sits outside the developer license's "as permitted in the Documentation".
  If it breaks:
  - removals pause, and mirrors stop dropping old tracks;
  - appends pause once a playlist reaches 1.5 times its window;
  - search, create and add keep working;
  - the daily canary shows it within a day.
- **The Apple Music sign-in expires at an undocumented time.** Reported lifetimes range
  from days to six months. `/health` turns `degraded` when Apple rejects the token, and warns
  from day 150. Signing in again takes one page.
- **One Apple account per instance.** Every playlist lives in that account's library.
- **Rate limits are undocumented.** After a 429 the server backs off that rail for ten
  minutes. Taking over a long playlist removes at most 25 tracks per sync for that reason.
- **Some songs are missing, or exist as a different version.** A track the Apple catalog
  doesn't have is left out, still counts toward the window, and is listed by
  `mirror_status` with the reason. The server never substitutes another recording. When
  only a title search finds a song and several versions fit, it takes the first and records
  the pick as ambiguous.
- **Tracks can leave the catalog.** A playlist track whose song Apple withdrew lists no
  catalog ID. The server counts it by its library ID, so it still expires normally. Such
  tracks may also be greyed out for listeners.
- **The playlist may list a different copy of a song.** If the library already holds the
  same recording from another release, Apple puts that copy in rather than the one sent. The
  server follows what Apple lists.

## Listening while the playlist changes

- **The top track is always the next to leave.** If it's playing when it's removed, the
  iPhone skips to the next track and the Mac plays it to the end (tested 2026-10-29). Neither
  stops.
- **Start a few tracks in, and don't shuffle.** The playlists say "skip the first 3-4
  tracks". That's a rule of thumb, not a guarantee: the top moves by the length of each
  arriving track, not one track at a time.
  - **Example:** "Fool's Overture" (Supertramp, 10:55) arriving pushes out 11 minutes from
    the top. That can be five 2-minute songs like "Song 2" (Blur, 2:01) at once.
  - **The other way round:** a 2-minute song arriving can push out an 11-minute track at the
    top. The window then sits up to one track under 24 hours.
- **Once ahead, you stay ahead.** The station's talk and ads move the top more slowly than
  you play music, so continuous listening keeps the lead. A long pause lets the top catch up.
- **Followers on other Apple accounts** see changes when their library syncs, not
  instantly. This hasn't been tested for playback.

## Sources

- **Polling can miss a track.** A feed that only reports the newest track can skip one if
  two change between polls (every 20 s). `mirror_status` reports coverage (music received per
  hour of wall time) and the shortest gap between arrivals. A webhook push, or a feed that
  lists the last few tracks, closes the gap without code changes.
- **Pushes without a Spotify ID are matched on title and artists only,** without an ISRC.
  Without a duration they count as four minutes until Apple's own duration is known.
- **The webhook secret travels in the URL.** Proxy logs can see it. The worst a leaked
  secret allows is injecting tracks into a mirror; rotating it fixes that.
- **Spotify playlists as a source need the playlist owner's sign-in.** Spotify's API no
  longer returns another user's playlist items. This source is planned, not built.

## Scope

- **No playback control,** queue, ratings or folders.
- **No reordering.** New tracks go at the end, old ones leave from the top.
- **Mirrors are configured in a file, not created by an agent.**
- **The server edits only playlists it created or a mirror took over.** Apple marks nearly
  every user playlist editable, so its flag isn't the gate. Every other playlist is
  read-only here.
