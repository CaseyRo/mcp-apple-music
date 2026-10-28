"""The Apple Music sign-in page, opened through a single-use link.

A browser can't send the MCP bearer key, so the link's random token is the gate:
GET consumes it to serve the page, and the page's one POST hands back the Music
User Token that MusicKit JS obtained from Apple.
"""

from __future__ import annotations

import json

# Apple's authorization checks which site asks, so the origin must travel; the link
# token must not (it could be replayed to swap in another account). The page also
# drops the token from its own URL on load, before MusicKit can read location.
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "origin",
    "X-Frame-Options": "DENY",
}

REFUSED = "This sign-in link was already used or has expired. Ask for a new one."


def link_url(public_url: str, service: str, raw: str) -> str:
    return f"{public_url.rstrip('/')}/auth/{service}?t={raw}"


def refused_page() -> str:
    return _shell("Link expired", f"<p>{REFUSED}</p>")


def apple_page(developer_token: str, link_token: str, version: str) -> str:
    config = json.dumps(
        {"dev": developer_token, "link": link_token, "build": version}
    ).replace("<", "\\u003c")
    return _shell(
        "Sign in to Apple Music",
        """
<p>This lets mcp-apple-music read your library and manage the playlists it creates.
It never touches playlists you made yourself.</p>
<button id="go" disabled>Sign in with Apple Music</button>
<p id="status" role="status">Loading MusicKit…</p>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" async></script>
<script>
const CONFIG = """
        + config
        + """;
history.replaceState(null, "", location.pathname);
const go = document.getElementById("go");
const status = document.getElementById("status");
document.addEventListener("musickitloaded", async () => {
  await MusicKit.configure({
    developerToken: CONFIG.dev,
    app: { name: "mcp-apple-music", build: CONFIG.build },
  });
  go.disabled = false;
  status.textContent = "";
});
go.addEventListener("click", async () => {
  go.disabled = true;
  status.textContent = "Waiting for Apple…";
  try {
    const token = await MusicKit.getInstance().authorize();
    const resp = await fetch(location.pathname, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t: CONFIG.link, music_user_token: token }),
    });
    status.textContent = (await resp.json()).message;
  } catch (err) {
    status.textContent = "Sign-in did not complete: " + err;
    go.disabled = false;
  }
});
</script>""",
    )


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 4rem auto; padding: 0 1rem; }}
  button {{ font: inherit; padding: 0.6rem 1.2rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""
