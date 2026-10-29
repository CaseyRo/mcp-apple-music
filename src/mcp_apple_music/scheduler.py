"""The mirror loop: one task per mirror, started from the app lifespan (design D3).

Each mirror polls its feed when due, and syncs when something arrived, when a
webhook woke it, or when its reconcile interval is up. After a 429 the rail it
hit backs off for ten minutes: the documented rail pauses syncs, the web rail
pauses only removals (appends go on, up to the engine's growth limit).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from .mirror import Engine, SyncResult, event
from .mirror_config import FeedSource, Mirror, WebhookSource
from .rails import (
    API,
    WEB,
    AppleError,
    RateLimited,
    UserTokenMissing,
    UserTokenRejected,
)
from .sources import Feed, SourceError

BACKOFF_SECONDS = 600
RETRY_SECONDS = 60
SIGN_IN_RETRY_SECONDS = 300


@dataclass
class _State:
    next_poll: float = 0.0
    next_reconcile: float = (
        0.0  # 0: sync on start, which sets up or adopts the playlist
    )
    not_before: float = 0.0
    dirty: bool = False


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        feed: Feed,
        mirrors: list[Mirror],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.feed = feed
        self.mirrors = [
            m for m in mirrors if isinstance(m.source, FeedSource | WebhookSource)
        ]
        self.clock = clock
        self.backoff_until: dict[str, float] = {}
        self._running = False
        self._state = {m.name: _State() for m in self.mirrors}
        self._wake = {m.name: asyncio.Event() for m in self.mirrors}
        for m in mirrors:
            if m not in self.mirrors:  # shortcut: until the Spotify owner source lands
                event("source_unsupported", m.name, source=m.source.type)

    def wake(self, name: str) -> None:
        if name in self._wake:
            self._state[name].dirty = True
            self._wake[name].set()

    def backing_off(self, rail: str) -> bool:
        return self.backoff_until.get(rail, 0.0) > self.clock()

    async def poll(self, m: Mirror) -> None:
        if not isinstance(m.source, FeedSource):
            return
        try:
            new = await self.feed.poll(m.name, m.source)
        except SourceError as e:
            self.engine.store.record_sync(m.name, f"feed: {e}")
            event("feed_failed", m.name, error=str(e))
            return
        if new and self.engine.ingest(m, new, "feed"):
            self._state[m.name].dirty = True

    async def sync(self, m: Mirror) -> SyncResult:
        """Sync now, honouring a back-off; raises what the engine raises."""
        if self.backing_off(API):
            raise RateLimited(
                API, 429, "backing off after a 429; retry in a few minutes"
            )
        try:
            return await self.engine.sync(m, removals=not self.backing_off(WEB))
        except RateLimited as e:
            self.backoff_until[e.rail] = self.clock() + BACKOFF_SECONDS
            event("backoff", m.name, rail=e.rail, seconds=BACKOFF_SECONDS)
            raise

    async def step(self, m: Mirror) -> float:
        """Do what is due for one mirror; returns seconds until something is next due."""
        st, now = self._state[m.name], self.clock()
        if isinstance(m.source, FeedSource) and now >= st.next_poll:
            st.next_poll = now + m.source.interval_seconds
            await self.poll(m)
        due = st.dirty or now >= st.next_reconcile
        if due and now >= st.not_before and not self.backing_off(API):
            st.dirty = False
            try:
                await self.sync(m)
                st.next_reconcile = now + m.reconcile_seconds
            except RateLimited:
                st.dirty = True
            except (UserTokenMissing, UserTokenRejected):
                st.dirty, st.not_before = True, now + SIGN_IN_RETRY_SECONDS
            except AppleError:
                st.dirty, st.not_before = True, now + RETRY_SECONDS
        polls = [st.next_poll] if isinstance(m.source, FeedSource) else []
        upcoming = [
            t
            for t in (
                st.next_reconcile,
                st.not_before,
                self.backoff_until.get(API, 0),
                *polls,
            )
            if t > now
        ]
        return max(1.0, min(upcoming) - now) if upcoming else 1.0

    async def _run_mirror(self, m: Mirror) -> None:
        while True:
            try:
                delay = await self.step(m)
            except Exception as e:  # noqa: BLE001 - the loop must outlive any one bug
                event("loop_error", m.name, error=repr(e))
                delay = RETRY_SECONDS
            ev = self._wake[m.name]
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
            except TimeoutError:
                pass
            ev.clear()

    async def run(self) -> None:
        if self._running:  # a second lifespan entry must not double the polling
            return
        self._running = True
        try:
            await asyncio.gather(*(self._run_mirror(m) for m in self.mirrors))
        finally:
            self._running = False
