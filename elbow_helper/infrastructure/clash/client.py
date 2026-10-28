"""Shared authenticated transport for the Clash of Clans API."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
import math
import time
from typing import Any
from typing import Mapping

import aiohttp


CLASH_API_BASE_URL = "https://api.clashofclans.com/v1"
DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Existing collectors fan out to at most eight workers. Leave headroom for
# overlapping jobs while bounding all requests through the application client.
MAX_CONCURRENT_REQUESTS = 16
MAX_CACHE_ENTRIES = 512


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def _cache_max_age(headers: Mapping[str, str]) -> int | None:
    ages: list[int] = []
    for directive in _header(headers, "Cache-Control").split(","):
        name, _, value = directive.strip().partition("=")
        name = name.strip().lower()
        if name in {"no-store", "no-cache"}:
            return None
        if name != "max-age":
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if not value.isascii() or not value.isdecimal():
            return None
        try:
            ages.append(int(value))
        except ValueError:
            return None
    return ages[0] if len(ages) == 1 and ages[0] > 0 else None


def _is_transient_error(error: BaseException | None) -> bool:
    if error is None:
        return False
    return isinstance(
        error,
        (
            asyncio.TimeoutError,
            aiohttp.ClientError,
            ValueError,
            OSError,
        ),
    )


@dataclass(frozen=True, slots=True)
class ClashResponse:
    """One completed Clash API request, including transport diagnostics."""

    status: int | None
    payload: Any
    headers: Mapping[str, str]
    attempts: int
    latency_ms: int | None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def transient(self) -> bool:
        return (
            self.status in DEFAULT_RETRY_STATUSES
            or _is_transient_error(self.error)
        )

    @property
    def payload_object(self) -> dict[str, Any] | None:
        return self.payload if isinstance(self.payload, dict) else None


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    response: ClashResponse
    expires_at: float


class ClashClient:
    """Own Clash transport, bounded GET caching, and the shared request limit."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = CLASH_API_BASE_URL,
        default_timeout_seconds: float = 20.0,
        default_attempts: int = 3,
        default_backoff_seconds: float = 1.0,
    ):
        self.api_key = str(api_key or "").strip() or None
        self.base_url = base_url.rstrip("/")
        self.default_timeout_seconds = max(0.1, default_timeout_seconds)
        self.default_attempts = max(1, default_attempts)
        self.default_backoff_seconds = max(0.0, default_backoff_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._cache: OrderedDict[tuple[str, str], _CachedResponse] = OrderedDict()
        self._in_flight: dict[tuple[str, str], asyncio.Task[ClashResponse]] = {}

    @property
    def configured(self) -> bool:
        return self.api_key is not None

    async def __aenter__(self) -> "ClashClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                headers = (
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key is not None
                    else {}
                )
                self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        pending = tuple(self._in_flight.values())
        self._in_flight.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._cache.clear()
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retry_delay(
        headers: Mapping[str, str],
        attempt: int,
        backoff_seconds: float,
        maximum_seconds: float,
        status: int | None = None,
    ) -> float | None:
        """Return the retry wait, or None when the server asks for too long."""
        retry_after = 0.0
        value = _header(headers, "Retry-After")
        try:
            retry_after = float(value or 0.0)
        except (TypeError, ValueError):
            if status == 429:
                try:
                    retry_after = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    pass
        if not math.isfinite(retry_after):
            retry_after = 0.0
        if status == 429 and retry_after > 0:
            # Either honour the full rate-limit wait or return the 429 now.
            return retry_after if retry_after <= maximum_seconds else None
        delay = retry_after if retry_after > 0 else backoff_seconds * attempt
        return max(0.0, min(maximum_seconds, delay))

    @staticmethod
    def _copy_response(response: ClashResponse, *, cached: bool = False) -> ClashResponse:
        return replace(
            response,
            payload=deepcopy(response.payload),
            headers=dict(response.headers),
            attempts=0 if cached else response.attempts,
            latency_ms=None if cached else response.latency_ms,
        )

    def _remember(
        self, key: tuple[str, str], response: ClashResponse, received_at: float,
    ) -> None:
        if response.status != 200 or response.error is not None:
            return
        max_age = _cache_max_age(response.headers)
        if max_age is None:
            return
        try:
            expires_at = received_at + max_age
        except OverflowError:
            return
        now = time.monotonic()
        if expires_at <= now:
            return
        for expired in [key for key, entry in self._cache.items() if entry.expires_at <= now]:
            del self._cache[expired]
        self._cache[key] = _CachedResponse(response, expires_at)
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[ClashResponse]) -> None:
        # A cancelled caller leaves its shielded flight running for other callers
        # and the cache. Retrieve unexpected exceptions even if nobody remains.
        if not task.cancelled():
            task.exception()

    async def get(
        self,
        path: str,
        *,
        attempts: int | None = None,
        timeout_seconds: float | None = None,
        backoff_seconds: float | None = None,
        maximum_backoff_seconds: float = 30.0,
        retry_statuses: frozenset[int] = DEFAULT_RETRY_STATUSES,
        fresh: bool = False,
    ) -> ClashResponse:
        """Share a JSON GET; fresh skips cache, but still joins an active flight.

        Every caller owns its payload and headers. Cache hits have attempts=0
        and latency_ms=None; flight waiters retain that flight's diagnostics.
        The first caller of a flight supplies its timeout and retry policy.
        """

        if not self.configured:
            return ClashResponse(
                status=None,
                payload=None,
                headers={},
                attempts=0,
                latency_ms=None,
            )

        key = ("GET", self._url(path))  # Includes the full path and query string.
        cached = self._cache.get(key)
        if cached is not None:
            if not fresh and cached.expires_at > time.monotonic():
                return self._copy_response(cached.response, cached=True)
            del self._cache[key]

        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_get(
                key,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                backoff_seconds=backoff_seconds,
                maximum_backoff_seconds=maximum_backoff_seconds,
                retry_statuses=retry_statuses,
            ))
            self._in_flight[key] = task
            task.add_done_callback(self._consume_task_exception)
        return self._copy_response(await asyncio.shield(task))

    async def _fetch_get(self, key: tuple[str, str], **kwargs: Any) -> ClashResponse:
        try:
            return await self._request_get(key, **kwargs)
        finally:
            if self._in_flight.get(key) is asyncio.current_task():
                del self._in_flight[key]

    async def _request_get(
        self,
        key: tuple[str, str],
        *,
        attempts: int | None,
        timeout_seconds: float | None,
        backoff_seconds: float | None,
        maximum_backoff_seconds: float,
        retry_statuses: frozenset[int],
    ) -> ClashResponse:
        request_attempts = max(1, attempts or self.default_attempts)
        request_timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else max(0.1, timeout_seconds)
        )
        request_backoff = (
            self.default_backoff_seconds
            if backoff_seconds is None
            else max(0.0, backoff_seconds)
        )
        session = await self._get_session()
        _, url = key
        last_response = ClashResponse(None, None, {}, 0, None)

        for attempt in range(1, request_attempts + 1):
            try:
                async with self._request_slots:
                    started_at = time.monotonic()
                    timeout = aiohttp.ClientTimeout(total=request_timeout)
                    async with session.get(url, timeout=timeout) as response:
                        received_at = time.monotonic()
                        latency_ms = int((received_at - started_at) * 1000)
                        headers = dict(response.headers)
                        payload: Any = None
                        parse_error: BaseException | None = None
                        try:
                            payload = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as error:
                            parse_error = error

                        last_response = ClashResponse(
                            status=response.status,
                            payload=payload,
                            headers=headers,
                            attempts=attempt,
                            latency_ms=latency_ms,
                            error=parse_error,
                        )
                should_retry = (
                    last_response.status in retry_statuses
                    or parse_error is not None
                )
                if not should_retry or attempt >= request_attempts:
                    self._remember(key, last_response, received_at)
                    return last_response
                delay = self._retry_delay(
                    headers,
                    attempt,
                    request_backoff,
                    maximum_backoff_seconds,
                    last_response.status,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                last_response = ClashResponse(
                    status=None,
                    payload=None,
                    headers={},
                    attempts=attempt,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    error=error,
                )
                if attempt >= request_attempts:
                    return last_response
                delay = self._retry_delay(
                    {},
                    attempt,
                    request_backoff,
                    maximum_backoff_seconds,
                )

            if delay is None:
                return last_response
            if delay > 0:
                await asyncio.sleep(delay)

        return last_response
