"""Reply identity and causal history survive a crowded request prefix."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
import json

from elbow_helper.features.agent.conversation.context import compile_context
from elbow_helper.features.agent.conversation.reply_context import prepare_reply_context
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.conversation.test_agent_context import _turn


class ReplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_causal_social_identity_survives_without_a_family_report(self):
        from elbow_helper.features.agent.research.member_provenance import member_target_issue
        context = self.context()
        human = SimpleNamespace(id=42, display_name='Synthetic target')
        original_get = context.guild.get_member
        context.guild.get_member = lambda identifier: human if identifier == 42 else original_get(identifier)
        first = context.state.authorized_history[0]
        first = replace(first, text=json.dumps({'resolved_entities': [
            {'kind': 'member', 'id': 42, 'display_name': 'Old name'}]}))
        context.state.authorized_history = (first,)
        await prepare_reply_context(context)
        self.assertEqual(context.state.resolved_members[42], 'Synthetic target')
        self.assertEqual(member_target_issue(context, {'target_member_id': 42}), '')
        self.assertNotEqual(member_target_issue(context, {'target_member_id': 77}), '')
    async def test_legacy_causal_member_lookup_is_structured_evidence_not_message_matching(self):
        context = self.context()
        first = context.state.authorized_history[0]
        evidence = json.dumps({'tool': 'find_discord_members', 'result': json.dumps({
            'status': 'resolved', 'members': [{'member_id': 42, 'display_name': 'Synthetic'}]})})
        first = replace(first, record=replace(first.record, evidence=(evidence,)))
        context.state.authorized_history = (first,)
        await prepare_reply_context(context)
        self.assertEqual(context.state.reply_context['conversation']['entities'][0]['id'], 42)

    async def test_authorized_family_selection_survives_as_structured_reply_metadata(self):
        from elbow_helper.features.agent.families.core import family_tool
        async def source(context, arguments):
            return {'rows': []}
        context = self.context()
        tool = family_tool('read_cwl_results', 'Read synthetic scores.', {}, source)
        report = await tool.handler(context, {'member_ids': [42], 'season': '2026-09'})
        first = context.state.authorized_history[0]
        first = replace(first, record=replace(first.record, report_ids=(report['report_id'],)))
        context.state.authorized_history = (first,)
        await prepare_reply_context(context)
        conversation = context.state.reply_context['conversation']
        self.assertEqual(conversation['resolved_inputs'][0]['selection'], {'member_ids': [42], 'season': '2026-09'})
        self.assertEqual(conversation['entities'], [{'kind': 'member', 'id': 42}])
        context.state.report_sources.clear()
        await prepare_reply_context(context)
        self.assertEqual(context.state.reply_context['conversation']['report_ids'], [])
        self.assertEqual(context.state.reply_context['conversation']['resolved_inputs'], [])

    def context(self):
        context = _context()
        context.source_message.reference = SimpleNamespace(message_id=1001, channel_id=91)
        context.state.authorized_history = (_turn(1, channel=91), _turn(2, channel=91))
        return context

    async def test_replied_bot_turn_retains_original_requester_and_visible_answer(self):
        context = self.context()
        replied = SimpleNamespace(id=1001, author=SimpleNamespace(id=999, display_name="Bot"),
                                  content="Visible notice", jump_url="source", reference=None)
        context = replace(context, attachment_sources=(replied,))
        await prepare_reply_context(context)
        anchor = context.state.reply_context
        self.assertEqual(anchor["author"]["member_id"], 999)
        self.assertEqual(anchor["conversation"]["requester_id"], 42)
        self.assertEqual(anchor["conversation"]["request"], "Question 1")
        self.assertEqual(anchor["conversation"]["delivered_answer"], "Answer 1")

    async def test_denied_reference_never_exposes_author_or_retained_body(self):
        context = self.context()
        context.source_message.reference.channel_id = 500
        await prepare_reply_context(context)
        self.assertEqual(context.state.reply_context, {"status": "unavailable", "message_id": 1001})

    async def test_unauthorized_history_cannot_supply_causal_request(self):
        context = self.context()
        context.state.authorized_history = ()
        await prepare_reply_context(context)
        self.assertNotIn("conversation", context.state.reply_context)

    async def test_old_reply_anchor_is_pinned_when_prefix_exceeds_soft_target(self):
        context = self.context()
        context.state.authorized_history = (_turn(1, channel=91), _turn(2, channel=91, payload_characters=50_000))
        await prepare_reply_context(context)
        compiled = compile_context(question="Can you give mine?", local_context="Unrelated GIF",
                                   context=context, tools=(), system_prompt="x" * 200_000)
        self.assertIn(1, compiled.selected_request_ids)
        self.assertIn('"request": "Question 1"', compiled.prompt)
        self.assertTrue(compiled.exceeds_target)

    async def test_recent_whole_history_survives_full_catalogue_without_reply(self):
        context = self.context()
        context.source_message.reference = None
        await prepare_reply_context(context)
        compiled = compile_context(question="Continue", local_context="", context=context,
                                   tools=(), system_prompt="x" * 200_000)
        self.assertEqual(compiled.selected_request_ids, (1, 2))
