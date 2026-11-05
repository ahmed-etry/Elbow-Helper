"""Player and clan profiles a feature asks for, fetched only through the collectors.

A feature names the account or clan and, optionally, how old a profile it accepts. The newest
profile a collector saw (scheduled or asked for) is served when it is new enough; otherwise one
request fetches it, shared by everyone asking for the same tag meanwhile. Each fetched profile
is stored in the family record in the background like a scheduled fetch. An outside account or
clan is stored only as an observation: it gets no clan stay or owner, and is never scheduled.
Every failure comes back as a ``Profile`` with a ``failure`` code, never as an exception.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
import json
import logging
from typing import Any
import zlib

from elbow_helper.data.store import now_us
from elbow_helper.data.store.clans import collect_requested_clan
from elbow_helper.data.store.players import collect_requested
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag


LOGGER = logging.getLogger(__name__)
SECOND_US = 1_000_000
# Newest profiles kept for serving, compressed (a few KB each): every family and
# linked account and clan, with room for the outside ones features ask about.
MAX_KEPT = 1024
PATHS = {"player": "/players/{}", "clan": "/clans/{}"}
# kind -> (collector, run scope, source kind)
RUNS = {"player": ("player_demand", "requested_accounts", "player_profile"),
        "clan": ("clan_demand", "requested_clans", "clan_profile")}


@dataclass(frozen=True, slots=True)
class Profile:
    """One profile a feature asked for, or why there is none. Times are UTC microseconds.

    ``failure`` is None, or one of: ``not_configured`` (no Clash access), ``invalid_tag``,
    ``not_found`` (HTTP 404), ``http_error`` (another status, in ``status``),
    ``network_error`` (no usable reply), ``invalid_payload`` or ``collector_error``.
    """

    kind: str
    tag: str
    payload: dict[str, Any] | None = None
    observed_at: int | None = None
    failure: str | None = None
    status: int | None = None
    attempts: int = 0
    latency_ms: int | None = None
    fetched: bool = False

    @property
    def ok(self) -> bool:
        return self.payload is not None


class ProfileDemand:
    """The collectors' on-demand profile path; features hold this instead of the Clash client."""

    def __init__(self, owner):
        self._owner = owner
        self._kept: OrderedDict[tuple[str, str], tuple[int, bytes]] = OrderedDict()
        self._flights: dict[tuple[str, str], asyncio.Task[Profile]] = {}
        self._waiting: dict[asyncio.Task[Profile], int] = {}
        self._unstored: dict[str, dict[str, Any]] = {kind: {} for kind in RUNS}
        self._writer: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(getattr(self._owner.clash_client, "configured", False))

    async def player(self, tag, *, max_age: float | None = None, attempts: int | None = None,
                     timeout_seconds: float | None = None, backoff_seconds: float | None = None,
                     maximum_backoff_seconds: float | None = None) -> Profile:
        """A player profile no older than ``max_age`` seconds (any kept one when None; 0 always fetches)."""
        return await self._ask("player", tag, max_age, attempts=attempts, timeout_seconds=timeout_seconds,
                               backoff_seconds=backoff_seconds, maximum_backoff_seconds=maximum_backoff_seconds)

    async def clan(self, tag, *, max_age: float | None = None, attempts: int | None = None,
                   timeout_seconds: float | None = None, backoff_seconds: float | None = None,
                   maximum_backoff_seconds: float | None = None) -> Profile:
        """A clan profile no older than ``max_age`` seconds (any kept one when None; 0 always fetches)."""
        return await self._ask("clan", tag, max_age, attempts=attempts, timeout_seconds=timeout_seconds,
                               backoff_seconds=backoff_seconds, maximum_backoff_seconds=maximum_backoff_seconds)

    def observed(self, kind: str, tag: str, response) -> None:
        """Keep a profile a scheduled pass fetched, so features can be served it.

        A reply from the client's cache carries no time of its own (no attempts), so it is not kept.
        """
        payload = getattr(response, "payload", None)
        if (getattr(response, "status", None) != 200 or getattr(response, "error", None) is not None
                or not getattr(response, "attempts", 0) or not isinstance(payload, dict)
                or normalize_player_tag(payload.get("tag")) != tag):
            return
        self._keep(kind, tag, payload, now_us())

    async def close(self) -> None:
        """Stop open fetches and finish storing the profiles already fetched."""
        flights = tuple(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        if self._writer is not None:
            await asyncio.gather(self._writer, return_exceptions=True)

    async def _ask(self, kind, tag, max_age, **request) -> Profile:
        canonical = normalize_player_tag(tag)
        if canonical is None:
            return Profile(kind, str(tag), failure="invalid_tag")
        kept = self._served(kind, canonical, max_age)
        if kept is not None:
            return kept
        if not self.configured:
            return Profile(kind, canonical, failure="not_configured")
        key = (kind, canonical)
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.create_task(self._fetch(kind, canonical, {
                name: value for name, value in request.items() if value is not None}))
            self._flights[key] = flight
            flight.add_done_callback(lambda task, key=key: self._landed(key, task))
        # A caller giving up leaves the shared fetch running for the others; once every
        # caller has given up, the fetch is cancelled, as a caller's own request would be.
        self._waiting[flight] = self._waiting.get(flight, 0) + 1
        try:
            result = await asyncio.shield(flight)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if flight.cancelled() and current is not None and not current.cancelling():
                # The collectors are closing; the caller itself was not cancelled.
                return Profile(kind, canonical, failure="collector_error")
            raise
        finally:
            self._waiting[flight] -= 1
            if not self._waiting[flight]:
                del self._waiting[flight]
                if not flight.done():
                    flight.cancel()
        return replace(result, payload=deepcopy(result.payload))

    def _landed(self, key, task) -> None:
        if self._flights.get(key) is task:
            del self._flights[key]
        if not task.cancelled():
            task.exception()

    def _served(self, kind, tag, max_age) -> Profile | None:
        entry = self._kept.get((kind, tag))
        if entry is None:
            return None
        at, packed = entry
        if max_age is not None and (max_age <= 0 or now_us() - at > max_age * SECOND_US):
            return None
        return Profile(kind, tag, json.loads(zlib.decompress(packed)), at, status=200)

    def _keep(self, kind, tag, payload, at) -> None:
        key = (kind, tag)
        entry = self._kept.get(key)
        if entry is not None and entry[0] >= at:
            return
        self._kept[key] = (at, zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
        self._kept.move_to_end(key)
        while len(self._kept) > MAX_KEPT:
            self._kept.popitem(last=False)

    async def _fetch(self, kind, tag, request) -> Profile:
        try:
            # Fresh: the client's cache cannot say when its reply was observed.
            response = await self._owner.clash_client.get(PATHS[kind].format(encode_clash_tag(tag)),
                                                          fresh=True, **request)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("Requested %s profile %s could not be fetched", kind, tag, exc_info=True)
            return Profile(kind, tag, failure="collector_error", fetched=True)
        at = now_us()
        payload = response.payload if isinstance(response.payload, dict) else None
        failure = ("network_error" if response.error is not None or response.status is None else
                   "not_found" if response.status == 404 else
                   "http_error" if response.status != 200 else
                   "invalid_payload" if payload is None else None)
        if failure is not None:
            return Profile(kind, tag, failure=failure, status=response.status, attempts=response.attempts,
                           latency_ms=response.latency_ms, fetched=True)
        if normalize_player_tag(payload.get("tag")) == tag:
            self._keep(kind, tag, payload, at)
            self._store(kind, tag, response)
        return Profile(kind, tag, payload, at, status=response.status, attempts=response.attempts,
                       latency_ms=response.latency_ms, fetched=True)

    def _store(self, kind, tag, response) -> None:
        # The scheduled clan pass owns family clans, rosters and stays included.
        if self._owner._closing or (kind == "clan" and any(
                getattr(clan, "tag", None) == tag for clan in self._owner.clans)):
            return
        self._unstored[kind][tag] = response
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._write())

    async def _write(self) -> None:
        # One run per batch keeps run rows few while features ask for many profiles at once.
        while any(self._unstored.values()):
            for kind, waiting in self._unstored.items():
                batch = list(waiting.items())
                waiting.clear()
                if batch:
                    await self._write_batch(kind, batch)

    async def _write_batch(self, kind, batch) -> None:
        collector, scope_kind, source_kind = RUNS[kind]
        collect = collect_requested if kind == "player" else collect_requested_clan
        store = self._owner.store

        async def operation(run):
            for ordinal, (tag, response) in enumerate(batch):
                await store.requested(run, ordinal)
                try:
                    await store.call(collect, run, ordinal, tag, response)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The run records this item as failed; the others are still stored.
                    LOGGER.debug("Requested %s profile %s could not be stored", kind, tag, exc_info=True)

        await self._owner._run(collector, scope_kind, [(source_kind, tag) for tag, _ in batch], operation)
