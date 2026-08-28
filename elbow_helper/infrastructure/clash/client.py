"""Shared authenticated transport for the Clash of Clans API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any
from typing import Mapping

import aiohttp


CLASH_API_BASE_URL = "https://api.clashofclans.com/v1"
DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


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


class ClashClient:
    """Own the shared Clash API session, authentication, and retry mechanics."""

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
    ) -> float:
        retry_after = 0.0
        try:
            retry_after = float(headers.get("Retry-After") or 0.0)
        except (TypeError, ValueError):
            retry_after = 0.0
        delay = retry_after if retry_after > 0 else backoff_seconds * attempt
        return max(0.0, min(maximum_seconds, delay))

    async def get(
        self,
        path: str,
        *,
        attempts: int | None = None,
        timeout_seconds: float | None = None,
        backoff_seconds: float | None = None,
        maximum_backoff_seconds: float = 30.0,
        retry_statuses: frozenset[int] = DEFAULT_RETRY_STATUSES,
    ) -> ClashResponse:
        """Perform a JSON GET request without assigning feature meaning to statuses."""

        if not self.configured:
            return ClashResponse(
                status=None,
                payload=None,
                headers={},
                attempts=0,
                latency_ms=None,
            )

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
        url = self._url(path)
        last_response = ClashResponse(None, None, {}, 0, None)

        for attempt in range(1, request_attempts + 1):
            started_at = time.monotonic()
            try:
                timeout = aiohttp.ClientTimeout(total=request_timeout)
                async with session.get(url, timeout=timeout) as response:
                    latency_ms = int((time.monotonic() - started_at) * 1000)
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
                        response.status in retry_statuses
                        or parse_error is not None
                    )
                    if not should_retry or attempt >= request_attempts:
                        return last_response
                    delay = self._retry_delay(
                        headers,
                        attempt,
                        request_backoff,
                        maximum_backoff_seconds,
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

            if delay > 0:
                await asyncio.sleep(delay)

        return last_response
