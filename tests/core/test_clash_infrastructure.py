from __future__ import annotations

import unittest
from collections import deque
from pathlib import Path
from typing import Any

from elbow_helper.domain.player_tags import canonical_player_tag
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> Any:
        del content_type
        return self.payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = deque(responses)
        self.closed = False
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return self.responses.popleft()

    async def close(self) -> None:
        self.closed = True


class ClashTagTests(unittest.TestCase):
    def test_canonical_tag_applies_formatting_without_validation(self) -> None:
        self.assertEqual(canonical_player_tag(" o2p "), "#02P")
        self.assertEqual(canonical_player_tag("#PLAYER"), "#PLAYER")

    def test_player_tag_validation_uses_the_official_alphabet(self) -> None:
        self.assertEqual(normalize_player_tag("p0y2l"), "#P0Y2L")
        self.assertIsNone(normalize_player_tag("#PLAYER"))
        self.assertIsNone(normalize_player_tag(""))

    def test_api_path_encoding_includes_the_tag_marker(self) -> None:
        self.assertEqual(encode_clash_tag("p0y2l"), "%23P0Y2L")


class ClashBoundaryTests(unittest.TestCase):
    def test_cogs_do_not_bypass_runtime_settings_or_shared_transport(self) -> None:
        cogs_directory = Path(__file__).resolve().parents[1] / "cogs"
        violations: list[str] = []

        for path in cogs_directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if "COC_API_KEY" in source or "api.clashofclans.com" in source:
                violations.append(str(path.relative_to(cogs_directory.parent)))

        self.assertEqual(violations, [])


class ClashClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_client_does_not_open_a_session(self) -> None:
        client = ClashClient(None)

        response = await client.get("/players/%23P0Y2L")

        self.assertIsNone(response.status)
        self.assertEqual(response.attempts, 0)
        self.assertIsNone(client._session)

    async def test_retryable_status_uses_one_shared_session(self) -> None:
        client = ClashClient(
            "token",
            base_url="https://example.test/v1",
            default_backoff_seconds=0,
        )
        session = _FakeSession(
            [
                _FakeResponse(429, {"reason": "rateLimit"}),
                _FakeResponse(200, {"tag": "#P0Y2L"}),
            ]
        )
        client._session = session  # type: ignore[assignment]

        response = await client.get(
            "/players/%23P0Y2L",
            attempts=2,
            backoff_seconds=0,
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.payload_object, {"tag": "#P0Y2L"})
        self.assertEqual(response.attempts, 2)
        self.assertEqual(
            session.urls,
            [
                "https://example.test/v1/players/%23P0Y2L",
                "https://example.test/v1/players/%23P0Y2L",
            ],
        )

        await client.close()
        self.assertTrue(session.closed)
        self.assertIsNone(client._session)
