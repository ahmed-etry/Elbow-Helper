"""Strict requests render complete typed fields, never generated prose."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.factual_answers import narrow_answer
from elbow_helper.features.agent.engine.flow import AnswerFlow
from elbow_helper.features.agent.engine.grounding import GroundingError, GROUNDING_UNAVAILABLE
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.wording import ACTION_EMPTY
from elbow_helper.infrastructure.ai import AgentToolDefinition
from features.agent.engine.test_agent_plan_flow import _context, _plan, _step, _model_step, _Model, _Session
from features.agent.discord_actions import test_current_channel_posts as posts
from elbow_helper.features.agent.discord_actions.messages import prepare_post


def players():
    return {"source": "clash_api", "kind": "player", "flags": {"status": "complete"}, "items": [
        {"tag": "#P0", "status": "ok", "data": {"name": "North"}},
        {"tag": "#P2", "status": "ok", "data": {"name": "South"}},
    ]}


def query(rows):
    return {"rows": rows, "row_count": len(rows), "truncated": False, "flags": {"status": "complete"}}


class StrictAnswerTests(unittest.TestCase):
    def test_player_tags_and_names_contain_only_validated_values(self):
        for question, expected in (("Tags only", "#P0\n#P2"), ("Only the tags please", "#P0\n#P2"),
                                   ("Names only", "North\nSouth"), ("just names", "North\nSouth")):
            self.assertEqual(narrow_answer(question, {"read": players()}), expected)

    def test_query_rows_and_compact_tables_use_the_same_fields(self):
        for rows in ([{"player_tag": "p0", "player_name": "North"},
                      {"player_tag": "#P2", "player_name": "South"}],
                     {"columns": ["player_tag", "player_name"], "rows": [["p0", "North"], ["#P2", "South"]]}):
            payload = {**query([]), "rows": rows, "row_count": 2}
            self.assertEqual(narrow_answer("tags only", {"read": payload}), "#P0\n#P2")
            self.assertEqual(narrow_answer("names only", {"read": payload}), "North\nSouth")

    def test_roster_counts_use_member_total_and_roster_fields(self):
        result = {"source": "clash_api", "kind": "clan", "items": [
            {"tag": "#Q0", "status": "ok", "data": {"members": 2, "memberList": [
                {"tag": "#P0", "name": "North"}, {"tag": "#P2", "name": "South"}]}}]}
        for request, expected in (("names only", "North\nSouth"), ("tags only", "#P0\n#P2"),
                                  ("count only", "2")):
            self.assertEqual(narrow_answer(request, {"read": result}), expected)
        result["items"][0]["data"]["members"] = 48
        self.assertEqual(narrow_answer("count only", {"read": result}), "48")
        with self.assertRaises(GroundingError):
            narrow_answer("names only", {"read": result})

    def test_zero_count_is_not_empty_or_false(self):
        self.assertEqual(narrow_answer("just the count", {"read": query([{"count": 0}])}), "0")
        for count in (True, -1, "2", 1.2, None):
            with self.subTest(count=count), self.assertRaises(GroundingError):
                narrow_answer("count only", {"read": query([{"count": count}])})

    def test_empty_lists_use_existing_empty_state_instead_of_empty_discord_message(self):
        for question in ("tags only", "names only"):
            self.assertEqual(narrow_answer(question, {"read": query([])}), ACTION_EMPTY)

    def test_invalid_names_and_tags_cannot_inject_extra_lines(self):
        for mode, value in (("name", "North\nHere is some advice"), ("name", None),
                            ("name", ""), ("tag", "#P0 Here is a joke"), ("tag", None)):
            with self.subTest(value=value), self.assertRaises(GroundingError):
                narrow_answer(f"{mode}s only", {"read": query([{mode: value}])})

    def test_incomplete_or_ambiguous_sources_never_fall_back_to_generated_values(self):
        for changes in ({"truncated": True}, {"next_offset": 2}, {"error": "denied"},
                        {"search_is_exhaustive": False}, {"flags": {"status": "partial"}}):
            with self.subTest(changes=changes), self.assertRaises(GroundingError):
                narrow_answer("tags only", {"read": {**players(), **changes}})
        for evidence in ({}, {"a": players(), "b": players()},
                         {"message": {"content": str(players())}},
                         {"read": query([{"tag": "#P0", "player_tag": "#P2"}])}):
            with self.subTest(evidence=evidence), self.assertRaises(GroundingError):
                narrow_answer("tags only", evidence)

    def test_wrong_accounts_failed_items_and_malformed_tables_are_rejected(self):
        with self.assertRaises(GroundingError):
            narrow_answer("names only for #P8", {"read": players()})
        payload = players()
        payload["items"][0]["status"] = "unavailable"
        with self.assertRaises(GroundingError):
            narrow_answer("tags only", {"read": payload})
        for rows in ({"columns": ["tag", "tag"], "rows": [["#P0", "#P2"]]},
                     {"columns": ["tag"], "rows": [["#P0", "extra"]]}, ["#P0"]):
            with self.subTest(rows=rows), self.assertRaises(GroundingError):
                narrow_answer("tags only", {"read": {**query([]), "rows": rows}})

    def test_conflicting_formats_fail_closed_and_open_questions_still_use_synthesis(self):
        with self.assertRaises(GroundingError):
            narrow_answer("tags only and names only", {"read": players()})
        self.assertIsNone(narrow_answer("Tell me about these players", {"read": players()}))


class StrictFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_fields_skip_answer_generation(self):
        for question, expected in (("tags only", "#P0\n#P2"), ("names only", "North\nSouth"),
                                   ("count only", "2")):
            with self.subTest(question=question):
                payload = query([{"count": 2}]) if question == "count only" else players()
                registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
                    "read_value", "Read synthetic values.", {"type": "object", "properties": {}}),
                    AsyncMock(return_value=payload))}
                session = _Session([_model_step(_plan([_step("read")]))], [])
                with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                    answer = await AgentService(_Model(session)).answer(
                        question=question, context=_context(), local_context="")
                self.assertEqual(answer, expected)
                self.assertEqual(len(session.calls), 1)

    async def test_generated_banter_is_discarded_and_missing_data_gets_no_repair_round(self):
        for evidence, expected in (({"read": players()}, "#P0\n#P2"), ({}, GROUNDING_UNAVAILABLE)):
            context = _context()
            context.state.request_text = "tags only"
            context.state.grounding_evidence = deepcopy(evidence)
            rounder = SimpleNamespace(advance=AsyncMock())
            flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                              rounder=rounder, runner=None, request_id=1)
            self.assertEqual(await flow._grounded_reply("#P8\nHere is some advice and banter"), expected)
            rounder.advance.assert_not_awaited()


class StrictPostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await posts.CurrentChannelPostTests.asyncSetUp(self)

    async def test_current_channel_post_cannot_bypass_strict_format(self):
        self.context.state.request_text = "names only"
        self.context.state.grounding_evidence = {"read": players()}
        result = await prepare_post(self.context, {"channel_id": 2, "text": "A joke instead of names"})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_with(self.context, "North\nSouth")
