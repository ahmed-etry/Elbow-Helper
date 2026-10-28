from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from elbow_helper.infrastructure.clash import client as transport
from elbow_helper.infrastructure.clash import ClashClient

from .test_clash_infrastructure import _FakeResponse, _FakeSession


class _GateResponse(_FakeResponse):
    def __init__(self, status=200, *, release=None, **kwargs):
        super().__init__(status, {"items": [{"value": 1}]}, **kwargs)
        self.entered = asyncio.Event()
        self.release = release or asyncio.Event()

    async def json(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().json(**kwargs)


class _ErrorResponse(_FakeResponse):
    def __init__(self, error, *, parsing=False):
        super().__init__(200, None, headers={"Cache-Control": "max-age=60"})
        self.error = error
        self.parsing = parsing

    async def __aenter__(self):
        if not self.parsing:
            raise self.error
        return self

    async def json(self, **kwargs):
        raise self.error


class ClashCacheTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        client = ClashClient("token", default_attempts=1, default_backoff_seconds=0)
        session = _FakeSession(responses)
        client._session = session
        self.addAsyncCleanup(client.close)
        return client, session

    async def test_exact_api_max_age_and_cache_diagnostics(self):
        for age in (60, 107, 120):
            with self.subTest(age=age):
                now = [10.0]
                clock = SimpleNamespace(monotonic=lambda: now[0])
                client, session = self.client([
                    _FakeResponse(200, {"version": 1}, headers={"Cache-Control": f"max-age={age}"}),
                    _FakeResponse(200, {"version": 2}),
                ])
                with patch.object(transport, "time", clock):
                    original = await client.get("/clans/%23P0Y")
                    now[0] += age - 0.01
                    cached = await client.get("/clans/%23P0Y")
                    self.assertEqual(cached.payload, original.payload)
                    self.assertEqual(cached.attempts, 0)
                    self.assertIsNone(cached.latency_ms)
                    self.assertEqual(len(session.urls), 1)
                    now[0] = 10.0 + age
                    expired = await client.get("/clans/%23P0Y")
                self.assertEqual(expired.payload, {"version": 2})
                self.assertEqual(expired.attempts, 1)
                self.assertIsNotNone(original.latency_ms)
                self.assertEqual(len(session.urls), 2)

    async def test_expiry_starts_at_headers_before_json_finishes(self):
        now = [10.0]
        gate = _GateResponse(headers={"Cache-Control": "max-age=60"})
        client, session = self.client([gate, _FakeResponse(200, {"next": True})])
        with patch.object(transport, "time", SimpleNamespace(monotonic=lambda: now[0])):
            first = asyncio.create_task(client.get("/slow-json"))
            await gate.entered.wait()
            now[0] = 20.0
            gate.release.set()
            await first
            now[0] = 70.0
            result = await client.get("/slow-json")
        self.assertEqual(result.payload, {"next": True})
        self.assertEqual(len(session.urls), 2)

    async def test_response_already_expired_during_parsing_is_not_retained(self):
        now = [0.0]
        gate = _GateResponse(headers={"Cache-Control": "max-age=1"})
        client, _ = self.client([gate])
        with patch.object(transport, "time", SimpleNamespace(monotonic=lambda: now[0])):
            pending = asyncio.create_task(client.get("/slow"))
            await gate.entered.wait()
            now[0] = 2.0
            gate.release.set()
            await pending
        self.assertFalse(client._cache)

    async def test_missing_invalid_and_uncacheable_directives_do_not_cache(self):
        for value in (None, "public", "max-age=0", "max-age=-1", "max-age=1.5",
                      "max-age=bad", "max-age=inf", 'max-age="60',
                      "max-age=60, max-age=120", "no-store, max-age=60",
                      "max-age=60, no-cache"):
            with self.subTest(value=value):
                headers = {} if value is None else {"Cache-Control": value}
                client, session = self.client([
                    _FakeResponse(200, {}, headers=headers), _FakeResponse(200, {}),
                ])
                await client.get("/uncached")
                result = await client.get("/uncached")
                self.assertEqual(result.attempts, 1)
                self.assertEqual(len(session.urls), 2)

    async def test_header_names_and_quoted_max_age_are_case_insensitive(self):
        client, session = self.client([
            _FakeResponse(200, {}, headers={"cache-control": 'public, MAX-AGE = "60"'}),
        ])
        await client.get("/cached")
        self.assertEqual((await client.get("/cached")).attempts, 0)
        self.assertEqual(len(session.urls), 1)

    async def test_only_status_200_is_cached(self):
        for status in (201, 204, 301, 404, 429, 500, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(status, {}, headers={"Cache-Control": "max-age=60"}),
                    _FakeResponse(200, {}),
                ])
                self.assertEqual((await client.get("/status")).status, status)
                self.assertEqual((await client.get("/status")).attempts, 1)
                self.assertEqual(len(session.urls), 2)

    async def test_transport_and_json_errors_are_not_cached(self):
        for error in (TimeoutError(), aiohttp.ClientConnectionError(), OSError(), ValueError("bad JSON")):
            with self.subTest(error=type(error).__name__):
                client, session = self.client([
                    _ErrorResponse(error, parsing=isinstance(error, ValueError)),
                    _FakeResponse(200, {}),
                ])
                failed = await client.get("/error")
                self.assertIs(failed.error, error)
                self.assertEqual((await client.get("/error")).attempts, 1)
                self.assertEqual(len(session.urls), 2)

    async def test_full_path_and_query_are_part_of_the_key(self):
        paths = ("/wars?limit=1", "/wars?limit=2", "/other?limit=1", "/wars?before=2&limit=1")
        client, session = self.client([
            _FakeResponse(200, {"path": path}, headers={"Cache-Control": "max-age=60"})
            for path in paths
        ])
        for path in paths:
            await client.get(path)
        for path in paths:
            self.assertEqual((await client.get(path)).payload, {"path": path})
        self.assertEqual((await client.get("wars?limit=1")).attempts, 0)
        self.assertEqual(len(session.urls), len(paths))

    async def test_network_and_cached_payloads_and_headers_are_isolated(self):
        client, _ = self.client([
            _FakeResponse(200, {"items": [{"value": 1}]}, headers={"Cache-Control": "max-age=60"}),
        ])
        first = await client.get("/copies")
        first.payload["items"][0]["value"] = 99
        first.headers.clear()
        second = await client.get("/copies")
        self.assertEqual(second.payload["items"][0]["value"], 1)
        second.payload["items"].clear()
        second.headers["Cache-Control"] = "max-age=0"
        third = await client.get("/copies")
        self.assertEqual(third.payload, {"items": [{"value": 1}]})
        self.assertEqual(third.headers["Cache-Control"], "max-age=60")

    async def test_identical_flights_share_success_and_isolate_waiter_copies(self):
        gate = _GateResponse(headers={"Cache-Control": "max-age=60"})
        client, session = self.client([gate])
        first = asyncio.create_task(client.get("/shared"))
        await gate.entered.wait()
        second = asyncio.create_task(client.get("/shared"))
        await asyncio.sleep(0)
        gate.release.set()
        original, waiter = await asyncio.gather(first, second)
        self.assertEqual(len(session.urls), 1)
        self.assertEqual((original.attempts, waiter.attempts), (1, 1))
        original.payload["items"][0]["value"] = 99
        original.headers.clear()
        self.assertEqual(waiter.payload["items"][0]["value"], 1)
        self.assertTrue(waiter.headers)
        self.assertEqual((await client.get("/shared")).attempts, 0)

    async def test_flight_failure_is_shared_but_next_call_retries(self):
        gate = _GateResponse(503, headers={"Cache-Control": "max-age=60"})
        client, session = self.client([gate, _FakeResponse(200, {})])
        first = asyncio.create_task(client.get("/failure", attempts=1))
        await gate.entered.wait()
        second = asyncio.create_task(client.get("/failure", attempts=3))
        await asyncio.sleep(0)
        gate.release.set()
        responses = await asyncio.gather(first, second)
        self.assertEqual([response.status for response in responses], [503, 503])
        self.assertEqual(len(session.urls), 1)
        self.assertEqual((await client.get("/failure")).status, 200)
        self.assertEqual(len(session.urls), 2)

    async def test_fresh_bypasses_cache_updates_it_and_joins_a_flight(self):
        gate = _GateResponse(headers={"Cache-Control": "max-age=60"})
        client, session = self.client([
            _FakeResponse(200, {"old": True}, headers={"Cache-Control": "max-age=60"}), gate,
        ])
        await client.get("/fresh")
        first = asyncio.create_task(client.get("/fresh", fresh=True))
        await gate.entered.wait()
        second = asyncio.create_task(client.get("/fresh", fresh=True))
        third = asyncio.create_task(client.get("/fresh"))
        await asyncio.sleep(0)
        gate.release.set()
        results = await asyncio.gather(first, second, third)
        self.assertTrue(all(result.payload == gate.payload for result in results))
        self.assertEqual(len(session.urls), 2)
        cached = await client.get("/fresh")
        self.assertEqual(cached.payload, gate.payload)
        self.assertEqual(cached.attempts, 0)

    async def test_uncacheable_fresh_result_does_not_restore_the_old_value(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session = self.client([
                    _FakeResponse(200, {"old": True}, headers={"Cache-Control": "max-age=60"}),
                    _FakeResponse(status, {"new": True}), _FakeResponse(200, {"next": True}),
                ])
                await client.get("/fresh")
                self.assertEqual((await client.get("/fresh", fresh=True)).status, status)
                self.assertEqual((await client.get("/fresh")).payload, {"next": True})
                self.assertEqual(len(session.urls), 3)

    async def test_expired_entries_are_dropped_before_oldest_live_entry(self):
        client, session = self.client([
            _FakeResponse(200, {"version": index}, headers={"Cache-Control": f"max-age={age}"})
            for index, age in enumerate((120, 1, 120, 120, 120))
        ])
        now = [0.0]
        with patch.object(transport, "MAX_CACHE_ENTRIES", 2), patch.object(
            transport, "time", SimpleNamespace(monotonic=lambda: now[0]),
        ):
            await client.get("/old")
            await client.get("/short")
            now[0] = 2.0
            await client.get("/third")
            self.assertEqual((await client.get("/old")).attempts, 0)
            await client.get("/fourth")
            self.assertEqual((await client.get("/old")).payload, {"version": 4})
            self.assertLessEqual(len(client._cache), 2)
        self.assertEqual(len(session.urls), 5)

    async def test_shared_limit_covers_different_gets_and_fresh_requests(self):
        release = asyncio.Event()
        limit = transport.MAX_CONCURRENT_REQUESTS
        responses = [_GateResponse(release=release) for _ in range(limit + 3)]
        client, session = self.client(responses)
        requests = [asyncio.create_task(client.get(f"/player/{index}", fresh=True))
                    for index in range(len(responses))]
        await asyncio.wait_for(asyncio.gather(*(response.entered.wait() for response in responses[:limit])), 2)
        self.assertEqual(len(session.urls), limit)
        release.set()
        self.assertTrue(all(response.ok for response in await asyncio.gather(*requests)))
        self.assertEqual(len(session.urls), len(responses))

    async def test_retry_after_is_honoured_and_other_backoff_stays_capped(self):
        cases = (
            (429, "2", 2), (429, "3", 3),
            (429, "Thu, 01 Jan 1970 00:00:02 GMT", 2),
            (429, "Thu, 01 Jan 1970 00:00:03 GMT", 3),
            (429, "invalid", 2), (429, "nan", 2), (429, "-1", 2),
            (500, "75", 3),
        )
        for status, value, expected in cases:
            with self.subTest(status=status, value=value):
                client, session = self.client([
                    _FakeResponse(status, {}, headers={"retry-after": value}), _FakeResponse(200, {}),
                ])
                with patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep, patch.object(
                    transport, "time", SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 0.0),
                ):
                    result = await client.get("/retry", attempts=2, backoff_seconds=2, maximum_backoff_seconds=3)
                sleep.assert_awaited_once_with(expected)
                self.assertEqual(result.status, 200)
                self.assertEqual(result.attempts, 2)
                self.assertEqual(len(session.urls), 2)

    async def test_excessive_retry_after_returns_shared_429_without_sleep_or_cache(self):
        yield_to_loop = asyncio.sleep
        for value in ("75", "Thu, 01 Jan 1970 00:01:00 GMT"):
            with self.subTest(value=value):
                gate = _GateResponse(429, headers={
                    "Retry-After": value, "Cache-Control": "max-age=60",
                })
                client, session = self.client([gate, _FakeResponse(200, {"next": True})])
                with patch.object(transport.asyncio, "sleep", new_callable=AsyncMock) as sleep, patch.object(
                    transport, "time", SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 0.0),
                ):
                    first = asyncio.create_task(client.get("/long-wait", attempts=3, maximum_backoff_seconds=3))
                    await gate.entered.wait()
                    second = asyncio.create_task(client.get("/long-wait", attempts=3, maximum_backoff_seconds=3))
                    await yield_to_loop(0)
                    gate.release.set()
                    responses = await asyncio.gather(first, second)
                    sleep.assert_not_awaited()
                    self.assertEqual(len(session.urls), 1)
                    for response in responses:
                        self.assertEqual(response.status, 429)
                        self.assertEqual(response.payload, gate.payload)
                        self.assertEqual(response.headers, gate.headers)
                        self.assertEqual(response.attempts, 1)
                        self.assertIsNone(response.error)
                        self.assertTrue(response.transient)
                    self.assertEqual((await client.get("/long-wait")).payload, {"next": True})
                    self.assertEqual(len(session.urls), 2)

    async def test_backoff_releases_the_shared_slot(self):
        sleeping, resume = asyncio.Event(), asyncio.Event()
        with patch.object(transport, "MAX_CONCURRENT_REQUESTS", 1):
            client, session = self.client([
                _FakeResponse(429, {}, headers={"Retry-After": "1"}),
                _FakeResponse(200, {"other": True}), _FakeResponse(200, {"retried": True}),
            ])

        async def pause(_delay):
            sleeping.set()
            await resume.wait()

        with patch.object(transport.asyncio, "sleep", side_effect=pause):
            first = asyncio.create_task(client.get("/retry", attempts=2))
            await sleeping.wait()
            other = await asyncio.wait_for(client.get("/other"), 2)
            self.assertEqual(other.payload, {"other": True})
            resume.set()
            self.assertEqual((await first).payload, {"retried": True})
        self.assertEqual(len(session.urls), 3)

    async def test_cancelling_one_waiter_does_not_cancel_the_flight(self):
        gate = _GateResponse()
        client, session = self.client([gate])
        first = asyncio.create_task(client.get("/cancel"))
        await gate.entered.wait()
        second = asyncio.create_task(client.get("/cancel"))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.release.set()
        self.assertTrue((await second).ok)
        self.assertEqual(len(session.urls), 1)

    async def test_close_cancels_pending_flights_and_clears_cache(self):
        gate = _GateResponse()
        client, session = self.client([
            _FakeResponse(200, {}, headers={"Cache-Control": "max-age=60"}), gate,
        ])
        await client.get("/cached")
        request = asyncio.create_task(client.get("/pending"))
        await gate.entered.wait()
        await client.close()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertFalse(client._cache)
        self.assertFalse(client._in_flight)
        self.assertTrue(session.closed)

    async def test_unexpected_flight_exception_does_not_block_the_next_call(self):
        client, session = self.client([
            _ErrorResponse(RuntimeError("synthetic")), _FakeResponse(200, {}),
        ])
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            await client.get("/unexpected")
        self.assertTrue((await client.get("/unexpected")).ok)
        self.assertEqual(len(session.urls), 2)
