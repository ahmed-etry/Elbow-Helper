from __future__ import annotations

import unittest
from collections import deque
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


class ClashClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_client_does_not_open_a_session(self) -> None:
        client = ClashClient(None)

        response = await client.get("/players/%23P0Y2L")

        self.assertIsNone(response.status)
        self.assertEqual(response.attempts, 0)
        self.assertIsNone(client._session)
