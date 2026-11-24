"""Author searches preserve scope through planning and execution."""
import unittest
from unittest.mock import AsyncMock, patch

from tests.agent.research.test_agent_search import _context
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.research.search import search_discord_messages


def author_plan(arguments):
    return {"goal": "Read a member's accessible history", "effort": "low", "output": "text",
            "steps": [
                {"id": "member", "capability": "find_discord_members",
                 "arguments": {"query": "synthetic-member"}, "reason": "Resolve member",
                 "depends_on": []},
                {"id": "history", "capability": "search_discord_messages",
                 "arguments": {"author_id": {"step": "member", "path": ["members", 0, "member_id"]},
                               **arguments},
                 "reason": "Read history", "depends_on": ["member"]},
            ]}


class SearchContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = build_agent_tools()

    async def test_multichannel_preflight_leaves_partial_access_to_the_search_reader(self):
        from elbow_helper.agent.loop.lookup_access import disclosure_issue
        context = _context()
        with patch('elbow_helper.agent.loop.lookup_access.require_evidence_access', AsyncMock()), \
             patch('elbow_helper.agent.loop.lookup_access.has_lookup_access', return_value=True), \
             patch('elbow_helper.agent.loop.lookup_access.accessible_message_channel', AsyncMock()) as access:
            result = await disclosure_issue(context, self.registry, {
                'capability': 'search_discord_messages', 'arguments': {'channel_ids': [100, 200]}})
        self.assertEqual(result, {})
        access.assert_not_awaited()

    async def test_empty_unfiltered_search_does_not_fetch_server_history(self):
        context = _context()
        result = await search_discord_messages(context, {"query": ""})
        self.assertIn("error", result)
        context.message_search.search.assert_not_awaited()
