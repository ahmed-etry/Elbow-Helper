from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.service import CoreAgentService, _valid_arguments
from elbow_helper.features.agent.tools import build_agent_tools
from elbow_helper.features.agent.tools.history import _document, read_conversation_history
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage


def _turn(message_id, question="original restriction", *, source=100, answer="delivered", generated=None):
    record = ConversationRecord(
        request_message_id=message_id, member_id=42, created_at="2026-09-17T00:00:00+00:00",
        question=question, generated_answer=generated or answer, delivered_answer=answer,
        local_context="", evidence=(), report_ids=(), reply_ids=(message_id + 1000,),
        delivery_complete=generated is None,
    )
    return ConversationTurn("short excerpt", frozenset({source}), record)


def _context(*turns):
    member = SimpleNamespace(id=42, display_name="Core tester", roles=[SimpleNamespace(id=next(iter(CORE)))])
    allowed = {100, 200}
    guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda _: member)
    channels = {value: SimpleNamespace(id=value, guild=guild, permissions_for=lambda _: SimpleNamespace(
        view_channel=True, read_message_history=True,
    )) for value in allowed}
    guild.get_channel_or_thread = lambda value: channels.get(value) if value in allowed else None
    bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=None))
    context = AgentRequestContext(
        bot=bot, guild=guild, member=member,
        source_message=SimpleNamespace(id=999, channel=channels[100], created_at=datetime.now(timezone.utc)),
        account_links=None, clan_health=None, message_search=None, history=tuple(turns),
    )
    return context, allowed


class AgentHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_finds_full_record_not_only_recent_excerpt(self):
        context, _ = _context(_turn(1, "Only players available all seven days"), _turn(2, "Unrelated"))
        result = await read_conversation_history(context, {"query": "SEVEN DAYS"})
        self.assertEqual(result["matching_retained_turns"], 1)
        self.assertEqual(result["results"][0]["request_message_id"], 1)
        self.assertIn("all seven days", result["results"][0]["content"])
        self.assertTrue(result["history_may_be_incomplete"])

    async def test_permissions_filter_results_and_counts_and_are_checked_on_every_call(self):
        context, allowed = _context(_turn(1, source=200), _turn(2))
        allowed.remove(200)
        result = await read_conversation_history(context, {})
        self.assertEqual(result["matching_retained_turns"], 1)
        self.assertEqual(context.state.source_channels, {100})
        allowed.add(200)
        result = await read_conversation_history(context, {"request_message_id": 1})
        self.assertEqual(len(result["results"]), 1)
        allowed.remove(200)
        with self.assertRaises(AgentAccessLost):
            await read_conversation_history(context, {})

    async def test_result_pages_cover_matching_turns_without_duplicates(self):
        context, _ = _context(*(_turn(index) for index in range(1, 9)))
        ids = []
        offset = 0
        while offset is not None:
            page = await read_conversation_history(context, {"offset": offset, "limit": 3})
            ids.extend(item["request_message_id"] for item in page["results"])
            offset = page["next_offset"]
        self.assertEqual(ids, list(range(8, 0, -1)))

    async def test_content_pages_reconstruct_long_record_without_losing_escaped_text(self):
        turn = _turn(1, ('"\\\nß' * 2000) + "last restriction")
        context, _ = _context(turn)
        fragments = []
        offset = 0
        while offset is not None:
            page = await read_conversation_history(context, {"request_message_id": 1, "content_offset": offset})
            item = page["results"][0]
            self.assertLessEqual(len(json.dumps(item, ensure_ascii=False)), 5000)
            fragments.append(item["content"])
            next_offset = item["next_content_offset"]
            if next_offset is not None:
                self.assertGreater(next_offset, offset)
            offset = next_offset
        self.assertEqual("".join(fragments), _document(turn))

    async def test_unicode_search_excerpt_contains_late_match(self):
        context, _ = _context(_turn(1, "ß" * 5000 + "important restriction"))
        page = await read_conversation_history(context, {"query": "important restriction"})
        self.assertIn("important restriction", page["results"][0]["content"])

    async def test_undelivered_generated_answer_is_neither_searchable_nor_returned(self):
        context, _ = _context(_turn(1, answer="sent", generated="sent and undelivered-secret"))
        result = await read_conversation_history(context, {"query": "undelivered-secret"})
        self.assertEqual(result["results"], [])
        result = await read_conversation_history(context, {"request_message_id": 1})
        content = json.loads(result["results"][0]["content"])
        self.assertEqual(content["answer"], "sent")
        self.assertFalse(content["delivery_complete"])

    async def test_unknown_id_cannot_read_a_different_conversation(self):
        context, _ = _context(_turn(1))
        result = await read_conversation_history(context, {"request_message_id": 2})
        self.assertEqual(result["results"], [])
        schema = build_agent_tools()["read_conversation_history"].definition.parameters
        for arguments in ({"conversation_id": 2}, {"limit": 6}, {"offset": -1}, {"content_offset": -1}):
            self.assertFalse(_valid_arguments(arguments, schema))

    async def test_excerpt_only_record_is_marked_incomplete(self):
        context, _ = _context(ConversationTurn("retained excerpt", frozenset({100}), retention_limited=True))
        result = await read_conversation_history(context, {})
        self.assertTrue(result["results"][0]["retention_limited"])
        self.assertIsNone(result["results"][0]["request_message_id"])

    async def test_stale_knowledge_turn_is_explicitly_historical(self):
        reference = ("policy@v1", "a" * 64)
        turn = ConversationTurn(
            "old policy", frozenset({100}), knowledge_refs=(reference,),
        )
        context, _ = _context(turn)
        context.state.stale_knowledge_refs.add(reference)
        result = await read_conversation_history(context, {})
        self.assertIn("HISTORICAL KNOWLEDGE WARNING", result["results"][0]["content"])

    async def test_actual_service_can_retrieve_history_and_continue_answer(self):
        context, _ = _context(_turn(1, "Keep these two accounts together", source=200))
        session = SimpleNamespace(advance=AsyncMock(side_effect=[
            AgentStep("", (AgentToolCall("lookup", "read_conversation_history", '{"query":"accounts"}'),), AgentUsage()),
            AgentStep("I will keep those accounts together.", (), AgentUsage()),
        ]))
        model = SimpleNamespace(create_agent_session=lambda **kwargs: session)
        answer = await CoreAgentService(model).answer(question="Use the earlier restriction", local_context="", context=context)
        self.assertIn("together", answer)
        self.assertIn("Keep these two accounts together", session.advance.await_args_list[1].args[0][0].content)
        self.assertEqual(context.state.source_channels, {200})
        self.assertEqual(len(context.state.evidence), 1)
