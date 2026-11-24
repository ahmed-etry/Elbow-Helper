"""Rejected replies, parked rosters and repeated exports retain safe recovery."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads.clash import read_clash

from tests.agent.request_fixtures import flow_context as _context


class CwlMembershipRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.reads.clash.require_evidence_access", AsyncMock())
        access.start()
        self.addCleanup(access.stop)
        self.payload = {"tag": CLANS["BE4"].tag, "name": "Brown Elbow IV", "members": 8,
                        "memberList": [{"tag": "#P0", "name": "Parked account", "townHallLevel": 3}]}
        self.client = SimpleNamespace(configured=True, get=AsyncMock(return_value=SimpleNamespace(
            ok=True, status=200, payload=self.payload)))
        self.context = replace(_context(), bot=SimpleNamespace(clash_client=self.client))

    async def test_placeholder_roster_is_withheld_for_every_projection_and_tag_spelling(self):
        for tag in ("BE4", CLANS["BE4"].tag):
            for projection in ({}, {"detail": "full"}, {"fields": ["members", "memberList"]},
                               {"fields": ["memberList.townHallLevel"]}):
                with self.subTest(tag=tag, projection=projection):
                    result = await read_clash(self.context, {"kind": "clan", "tags": [tag], **projection})
                    row = result["items"][0]
                    self.assertNotIn("members", row["data"])
                    self.assertNotIn("memberList", row["data"])
                    self.assertEqual(row["member_cohort"]["basis"], "seasonal_cwl")
                    self.assertNotIn("Parked account", json.dumps(result))
        self.assertEqual(self.payload["members"], 8)
        self.assertEqual(len(self.payload["memberList"]), 1)
