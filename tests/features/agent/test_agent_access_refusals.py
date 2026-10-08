"""Lookup refusals explain access to the model without ending a plan."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.access import lookup_level


class AccessRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_lookup_is_data_and_does_not_run(self):
        handler = AsyncMock()
        context = SimpleNamespace(guild=object(), member=SimpleNamespace(id=1))
        with (
            patch("elbow_helper.features.agent.access.require_evidence_access", AsyncMock()),
            patch("elbow_helper.features.agent.access.has_access_requirements", return_value=False),
        ):
            result = await lookup_level("lead")(handler)(context, {})
        self.assertEqual(result, {"error": "This needs lead access.", "required_access": ["lead"]})
        handler.assert_not_awaited()
