"""Scripted Apple endpoints for httpx.MockTransport, and a settable clock."""

from __future__ import annotations

import base64
import json

import httpx

BUNDLE = "/assets/index~6da982354d.js"


class Apple:
    """Routes (method, path) to scripted responses; the last one repeats."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.on("GET", "/v1/me/storefront", 200, {"data": [{"id": "nl"}]})

    def on(
        self, method: str, path: str, status: int, body=None, *, text=None, headers=None
    ) -> None:
        kw = {"json": body} if body is not None else {"text": text or ""}
        resp = httpx.Response(status, headers=headers, **kw)
        self.routes.setdefault((method, path), []).append(resp)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes[(request.method, request.url.path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sent(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def web_token(exp: int, n: int = 1) -> str:
    """Shaped like the real one (verified 2026-10-28): typ-first header, AMPWebPlay."""
    header = b64({"typ": "JWT", "alg": "ES256", "kid": "WebPlayKid"})
    claims = b64({"iss": "AMPWebPlay", "iat": exp - 70 * 86_400, "exp": exp, "n": n})
    return f"{header}.{claims}.c2lnbmF0dXJl"


def serve_web_player(apple: Apple, token: str) -> None:
    """music.apple.com as captured: browse 301s, the page names the bundle."""
    apple.on(
        "GET", "/us/browse", 301, headers={"Location": "https://music.apple.com/us/new"}
    )
    apple.on(
        "GET", "/us/new", 200, text=f'<script type="module" src="{BUNDLE}"></script>'
    )
    apple.on("GET", BUNDLE, 200, text=f'const Do="2644.9.0-external",qa="{token}";')
