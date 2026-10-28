"""What both Apple rails share: errors, results and per-rail throttle state.

There are two rails. ``api`` is the documented api.music.apple.com with our own
developer token. ``web`` is the web player's amp-api.music.apple.com, used only for
what the documented API can't do. Every result and error names its rail.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

API = "api"
WEB = "web"

# Apple throttles on a rolling window of up to ~60 minutes and sends no Retry-After,
# so an empty answer this soon after a 429 may be the throttle, not a real "none".
THROTTLE_WINDOW_SECONDS = 3600


class AppleError(Exception):
    """Apple refused or failed a request. ``status`` is None when nothing came back."""

    def __init__(self, rail: str, status: int | None, reason: str) -> None:
        super().__init__(
            f"{rail} rail: " + (f"HTTP {status}: " if status else "") + reason
        )
        self.rail = rail
        self.status = status
        self.reason = reason


class RateLimited(AppleError):
    """HTTP 429. Never a "not found": the request did not run."""


class UserTokenMissing(AppleError):
    """No usable Music User Token; nothing was sent."""


class UserTokenRejected(AppleError):
    """Apple answered 401/403 to the Music User Token; it is now marked invalid."""


class RailUnavailable(AppleError):
    """The rail itself can't be used right now (e.g. no web-player token)."""


@dataclass
class Read:
    """A read's resources, plus whether an empty answer may be a throttle."""

    data: list[dict] = field(default_factory=list)
    possibly_throttled: bool = False


@dataclass
class Write:
    """A write Apple accepted: which rail carried it, its status, any new id."""

    rail: str
    status: int
    id: str | None = None


class Throttle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_429: float | None = None

    def hit(self) -> None:
        self._last_429 = self._clock()

    def clear(self) -> None:
        self._last_429 = None

    def recent(self) -> bool:
        return (
            self._last_429 is not None
            and self._clock() - self._last_429 < THROTTLE_WINDOW_SECONDS
        )

    def label(self, data: list[dict]) -> Read:
        # Only a non-empty answer proves the rail is really answering again.
        if data:
            self.clear()
        return Read(data, possibly_throttled=not data and self.recent())


def error_reason(resp: httpx.Response) -> str:
    """Apple's own reason from its ``errors`` array, else a body snippet."""
    try:
        err = resp.json()["errors"][0]
        return err.get("detail") or err.get("title") or err.get("code") or "error"
    except (ValueError, KeyError, IndexError, TypeError):
        return resp.text.strip()[:200] or resp.reason_phrase or "error"


class RailClient:
    """One rail's HTTP plumbing: auth headers, error mapping, ``next`` pagination."""

    rail: str
    base_url: str

    def __init__(
        self,
        user_token: Callable[[], str | None],
        on_user_token_rejected: Callable[[], None] = lambda: None,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._user_token = user_token
        self._on_rejected = on_user_token_rejected
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=30, transport=transport, headers=headers
        )
        self.throttle = throttle or Throttle()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _auth(self, user: bool) -> dict[str, str]:
        """Authorization headers; the Music User Token only when ``user``."""
        raise NotImplementedError

    def _drop_credential(self) -> bool:
        """Called on a 401; return True to retry once with a fresh credential."""
        return False

    def _music_user_token(self) -> str:
        mut = self._user_token()
        if not mut:
            raise UserTokenMissing(
                self.rail, None, "no valid Apple Music user token; sign in again"
            )
        return mut

    async def _request(
        self, method: str, path: str, *, user: bool = False, ok_404: bool = False, **kw
    ) -> httpx.Response | None:
        for attempt in (1, 2):
            headers = await self._auth(user)
            try:
                resp = await self._http.request(method, path, headers=headers, **kw)
            except httpx.HTTPError as e:
                raise AppleError(self.rail, None, f"request failed: {e!r}") from e
            if not (
                resp.status_code == 401 and attempt == 1 and self._drop_credential()
            ):
                break

        if resp.status_code == 429:
            self.throttle.hit()
            raise RateLimited(self.rail, 429, "rate limited by Apple; retry later")
        if user and resp.status_code in (401, 403):
            self._on_rejected()
            raise UserTokenRejected(
                self.rail, resp.status_code, f"{error_reason(resp)}; sign in again"
            )
        if ok_404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise AppleError(self.rail, resp.status_code, error_reason(resp))
        return resp

    async def _pages(self, path: str, **params) -> list[dict]:
        """Follow Apple's ``next`` links. A 404 on a library listing means "empty"."""
        out: list[dict] = []
        next_path: str | None = path
        while next_path:
            resp = await self._request(
                "GET", next_path, user=True, ok_404=True, params=params
            )
            if resp is None:
                break
            body = resp.json()
            out.extend(body.get("data", []))
            # `next` carries its own query; httpx would drop it for params={}.
            next_path, params = body.get("next"), None
        return out
