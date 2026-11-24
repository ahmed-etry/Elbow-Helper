"""Reply targets, citations and access remain safe during recovery."""

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.safety.access import assert_answer_scope
from elbow_helper.agent.discord_actions.source_references import ground_answer
from tests.agent.request_fixtures import flow_context as _context


class ReplyScopeSafetyTests(unittest.IsolatedAsyncioTestCase):

    async def test_late_repair_links_bind_to_observed_channel_before_scope_guard(self):
        context = _context()
        message = {"message_id": 101, "author_id": 202, "author": "Member",
                   "content": "Review the promotion.", "source": "https://discord.com/channels/3/91/101"}
        evidence = {"history": {"messages": [message]}}
        bad = "https://discord.com/channels/3/92/101"
        draft = f"Member suggested a review ([message]({bad})). Correction: {message['source']}"
        answer = ground_answer(draft, evidence)
        self.assertNotIn(bad, answer)
        self.assertEqual(answer.count(message["source"]), 2)
        with (patch("elbow_helper.agent.safety.access.require_evidence_access", new_callable=AsyncMock),
              patch("elbow_helper.agent.safety.access.accessible_message_channel", new=AsyncMock(
                  side_effect=lambda _, channel_id, **kwargs: context.source_message.channel if channel_id == 91 else None))):
            await assert_answer_scope(context, answer)
