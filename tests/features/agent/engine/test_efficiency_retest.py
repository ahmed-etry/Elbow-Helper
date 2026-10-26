"""Exercise the second efficiency failures with real scope, scans and output checks."""

from copy import deepcopy
from dataclasses import replace
from features.agent.engine.helpers import LegacyCatalogueMixin

import unittest

from elbow_helper.configuration.channels import LEADERSHIP_CATEGORY
from elbow_helper.configuration.roles import CO_APPLICANT_ROLE_ID, CO_LEADER_ROLE_ID, LEAD
from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.features.agent.engine.evidence_delta import EvidenceDelta
from elbow_helper.features.agent.engine.social_counts import correct_social_counts
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.results import compact_result
from elbow_helper.features.agent.plan.planning import request_plan
from elbow_helper.features.agent.plan.checker import valid_arguments
from elbow_helper.features.agent.research.directory import scoped_channel_directory
from elbow_helper.features.agent.research.history import read_discord_channel_history, read_message_context
from features.agent.engine.helpers import social_step
from features.agent.engine.test_efficiency import planned, step
from features.agent.engine import test_request_cost as cost
from features.agent.research.test_agent_search import _context
from features.agent.research.test_history_text_filter import history
from features.agent import test_evidence_scope as scope_cases
from features.agent.test_evidence_scope import Channel, Role

response = cost.response


class EfficiencyRetestTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_cursor_only_continuation_preserves_window_and_rejects_scope_changes(self):
        context = _context()
        rows = history(context, [(1, "post one", (), ()), (2, "post two", (), ())])
        first = await read_discord_channel_history(context, {
            "channel_id": 100, "after": "2026-09-01", "limit": 1,
        })
        arguments = {"channel_id": 100, "cursor": first["coverage"]["next_cursor"], "limit": 1}
        for changed in ({"channel_id": 200}, {"after": "2026-08-01"}, {"author_id": 2}):
            result = await read_discord_channel_history(context, {**arguments, **changed})
            self.assertIn("error", result)
        self.assertEqual(context.message_search.history_page.await_count, 1)
        second = await read_discord_channel_history(context, arguments)
        self.assertEqual(second["messages"][0]["message_id"], rows[1].message_id)
        self.assertEqual(second["coverage"]["requested_after"], first["coverage"]["requested_after"])
        other_request = _context()
        self.assertIn("error", await read_discord_channel_history(other_request, arguments))

    async def test_chunk_continuation_reaches_old_updates_without_lost_messages(self):
        context = _context()
        records = [(1, "chatter " * 120, (), ()) for _ in range(190)]
        records += [(1, "Recruitment post was deleted", (), ())]
        rows = history(context, records)
        arguments = {"channel_id": 100, "after": "2026-09-01", "pages": 8, "limit": 100}
        broad = await read_discord_channel_history(context, arguments)
        self.assertTrue(broad["coverage"]["stopped_at_message_budget"])
        self.assertEqual(context.message_search.history_page.await_count, 1)
        found = [row["message_id"] for row in broad["messages"]]
        cursor = broad["coverage"]["next_cursor"]
        while cursor:
            chunk = await read_discord_channel_history(context, {
                "channel_id": 100, "cursor": cursor, "limit": 100, "pages": 8,
            })
            found.extend(row["message_id"] for row in chunk["messages"])
            cursor = chunk["coverage"]["next_cursor"]
        self.assertEqual(found, [row.message_id for row in rows])

    async def test_cursor_state_survives_checked_plan_tool_isolation(self):
        context = _context()
        # Use the complete service context with the history transport fixture.
        from features.agent.engine.test_agent_plan_flow import _context as flow_context
        live = flow_context()
        channel = live.source_message.channel
        channel.id = 100
        context.source_message.channel = context.guild.get_channel_or_thread(100)
        rows = history(context, [(1, "post one", (), ()), (1, "post two", (), ())])
        live.source_message.id = context.source_message.id
        live.source_message.created_at = context.source_message.created_at
        live = replace(live, message_search=context.message_search)
        registry = build_agent_tools()
        registry = {"read_discord_channel_history": registry["read_discord_channel_history"]}
        flow, _, _, _ = self.flow([
            planned(step("first", "read_discord_channel_history", {
                "channel_id": 100, "after": "2026-09-01", "limit": 1,
            }), step("next", "read_discord_channel_history", {
                "channel_id": 100, "cursor": {"step": "first", "path": ["coverage", "next_cursor"]},
                "limit": 1,
            })), response("The two recruitment posts are available."),
        ], registry=registry, context=live)
        await flow.run()
        self.assertEqual(flow.completed_results["next"]["messages"][0]["message_id"], rows[1].message_id)

    async def test_lead_plus_destination_directory_keeps_leadership_discussion_reachable(self):
        scope_cases.ScopeTests.setUp(self)
        self.asker.roles.append(Role(CO_LEADER_ROLE_ID))
        self.peer.roles.append(Role(CO_APPLICANT_ROLE_ID))
        self.guild.roles.append(Role(CO_APPLICANT_ROLE_ID))
        discussion = Channel(300, self.guild, {10, 99}, category=LEADERSHIP_CATEGORY)
        discussion.name = "cross-room"
        restricted = Channel(301, self.guild, {10, 99}, level=LEAD)
        restricted.name = "restricted-lead-room"
        self.guild.channels += [discussion, restricted]
        directory = await scoped_channel_directory(self.context)
        self.assertIn('"300":"cross-room"', directory)
        self.assertNotIn("301", directory)
        self.assertNotIn("lead", self.context.state.scope.levels)
        self.assertEqual(discussion.reads, 0)
        self.assertFalse(self.context.state.scope_audit.private_candidates)
        self.context.state.channel_directory = directory
        registry = {"read_message_context": build_agent_tools()["read_message_context"]}
        flow, _, model, _ = self.flow([
            planned(step("thread", "read_message_context", {"channel_id": 300, "message_id": 123})),
            response("The visible discussion is available."),
        ], registry=registry, context=self.context)
        await flow.run()
        self.assertIn(directory, model.request["prompt"])
        self.assertIn("messages", flow.completed_results["thread"])
        self.assertEqual(discussion.reads, 1)
        self.asker.roles.clear()
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)
        # The same requester in a member destination cannot discover or read it.
        self.asker.roles.append(Role(CO_LEADER_ROLE_ID))
        self.message.channel = self.member_chat
        public = replace(self.context, state=type(self.context.state)())
        self.assertNotIn("300", await scoped_channel_directory(public))
        self.assertIn("error", await read_message_context(public, {"channel_id": 300, "message_id": 123}))
        self.assertEqual(discussion.reads, 1)

    async def test_social_final_boundary_corrects_counts_without_another_round(self):
        for output in (social_step("five letters, no context. I'll take the win."),
                       response("five letters, no context. I'll take the win.")):
            flow, session, _, _ = self.flow([output], question="thanks")
            flow.context.state.request_text = "lol"
            self.assertEqual(await flow.run(), "three letters, no context. I'll take the win.")
            self.assertEqual(len(session.calls), 1)

    async def test_missing_step_id_does_not_spend_a_model_correction_round(self):
        flow, session, _, _ = self.flow([
            planned({"capability": "read_clash", "arguments": {}, "depends_on": []}),
            response("Synthetic league."),
        ])
        self.assertEqual(await flow.run(), "Synthetic league.")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(flow.runner.tool_calls, 1)


class SocialCountTests(unittest.TestCase):
    def test_literal_letters_digits_numbers_and_quoted_subjects(self):
        cases = [
            ("lol", "Five letters. Big speech.", "Three letters. Big speech."),
            ("a 123 45", "Nine letters; 8 digits; seven numbers.", "One letter; 5 digits; two numbers."),
            ("anything", '`lol` has five letters.', '`lol` has three letters.'),
            ("e\u0301", "three characters, two letters", "two characters, one letter"),
            ("lol", "three letters, no context.", "three letters, no context."),
            ("lol", "Five-letter reply. Big speech.", "Three-letter reply. Big speech."),
            ("lol", "I have two words for you.", "I have two words for you."),
            ("lol", "lol has five letters.", "lol has three letters."),
        ]
        for message, answer, expected in cases:
            with self.subTest(answer=answer):
                self.assertEqual(correct_social_counts(answer, message), expected)


class RepeatedEvidenceTests(unittest.TestCase):
    def test_object_alternatives_preserve_shared_fields_and_base_constraints(self):
        schema = {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["once", "weekly"]},
            "at": {"type": "string"}, "day": {"type": "integer", "minimum": 0, "maximum": 6},
        }, "anyOf": [
            {"properties": {"kind": {"enum": ["once"]}}, "required": ["at"]},
            {"properties": {"kind": {"enum": ["weekly"]}}, "required": ["day"]},
        ], "additionalProperties": False}
        self.assertTrue(valid_arguments({"kind": "once", "at": "2026-10-22"}, schema))
        self.assertTrue(valid_arguments({"kind": "weekly", "day": 3}, schema))
        self.assertFalse(valid_arguments({"kind": "once"}, schema))
        self.assertFalse(valid_arguments({"kind": "weekly", "day": 30}, schema))
        self.assertFalse(valid_arguments({"kind": "once", "at": 3}, schema))
        self.assertFalse(valid_arguments({"kind": "once", "at": "today", "unknown": True}, schema))

    def test_missing_internal_step_id_is_filled_without_changing_read_or_dependencies(self):
        call = planned(step("step_2", "find_discord_members", {"query": "Synthetic"}), {
            "capability": "read_linked_accounts", "arguments": {
                "member_ids": {"step": "step_2", "path": ["members", "*", "member_id"]},
            }, "depends_on": ["step_2"],
        })
        plan = request_plan(call)
        self.assertEqual(plan["steps"][1]["id"], "step_2_auto")
        self.assertEqual(plan["steps"][1]["depends_on"], ["step_2"])
        self.assertEqual(plan["steps"][1]["arguments"]["member_ids"]["step"], "step_2")
        # Other malformed fields still pass to the normal checker unchanged.
        self.assertNotIn("arguments", request_plan(planned({"capability": "read_clash"}))["steps"][0])

    def test_search_context_overlap_is_referenced_and_changed_text_is_retained(self):
        delta = EvidenceDelta()
        rows = [{"message_id": index, "channel_id": 100, "author_id": 1, "author": "Member",
                 "content": "Original evidence " * 20, "timestamp": "2026-10-21",
                 "source": f"https://discord.com/channels/1/100/{index}"} for index in range(12)]
        first = {"search": {"matches": compact_result(rows)}}
        snapshot = deepcopy(first)
        delta.prepare(first)
        context = {key: item for key, item in rows[3].items() if key != "channel_id"}
        context["is_requested_message"] = True
        emitted, _ = delta.prepare({"context": {"channel_id": 100, "messages": [context]}})
        self.assertEqual(emitted["context"]["messages"], [])
        self.assertEqual(emitted["context"]["reused_messages"][0]["reference"]["path"], ["matches", "rows", 3])
        self.assertTrue(emitted["context"]["reused_messages"][0]["is_requested_message"])
        changed, _ = delta.prepare({"edit": {"messages": [{**context, "content": "Edited evidence"}]}})
        self.assertEqual(changed["edit"]["messages"][0]["content"], "Edited evidence")
        self.assertEqual(first, snapshot)

    def test_reworded_knowledge_search_reuses_text_and_preserves_unread_pages(self):
        delta = EvidenceDelta()
        result = {"sections": [{"section_id": "entry", "title": "Entry", "body": "Gameplay review required."}],
                  "next_offset": 8}
        delta.prepare({"first": result}, {"first": "query one"})
        visible, changed = delta.prepare({"again": result}, {"again": "query two"})
        self.assertTrue(changed)
        self.assertEqual(visible["again"]["already_opened_titles"], ["Entry"])
        self.assertEqual(visible["again"]["next_offset"], 8)
        continued, changed = delta.prepare({"page": {**result, "sections": [{"title": "Missing", "body": "New fact"}]}})
        self.assertTrue(changed)
        self.assertTrue(continued["page"]["sections"])
