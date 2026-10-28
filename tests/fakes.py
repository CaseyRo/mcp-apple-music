"""Scripted Apple endpoints for httpx.MockTransport, and a settable clock."""

from __future__ import annotations

import httpx


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
