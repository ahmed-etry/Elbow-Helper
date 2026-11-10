"""Exercise production answer sessions and provider serialization with synthetic reads.

The transport is scripted; these test the evidence/instruction boundary, not a
live model's writing quality. No held-out run wording is used as an oracle.
"""

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import base64
from io import BytesIO
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.agent.conversation.context import build_history_checkpoint
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.plan.format import IMAGE_EVIDENCE_INSTRUCTION, WRITER_INSTRUCTION, answer_instructions
from elbow_helper.infrastructure.ai import AgentToolDefinition, DeepSeekTextClient
from tests.agent.engine.test_agent_plan_flow import _context, _plan
from tests.agent.engine.test_writer_tables import expand
from tests.agent.conversation.test_agent_context import _turn


def provider_response(content="", calls=()):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content,
                                  tool_calls=list(calls)), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                              prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1000),
        id="synthetic-composition", model="synthetic-provider",
    )


def call(name, arguments):
    return {"id": "synthetic-call", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def client(provider):
    transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=provider))), close=AsyncMock())
    transport.with_options = lambda **_: transport
    model = DeepSeekTextClient(None)
    model._client = transport
    return model


def writer_payload(options):
    content = options["messages"][1]["content"]
    text = content[0]["text"] if isinstance(content, list) else content
    prompt, evidence = text.split("\nAvailable evidence:\n", 1)
    prompt = json.loads(prompt)
    return prompt, json.loads(prompt["context"]), json.loads(evidence)["results"]


class AnswerCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_clan_policy_uses_current_host_heading_and_keeps_unreadable_rule_unknown(self):
        from elbow_helper.agent.reads.reference import TOOLS
        from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
        from elbow_helper.agent.engine.models import EvidenceScope

        for title, visibility in (('Cat battle plan revision 8', 'public'),
                                  ('Renamed instructions for Cat', 'public'),
                                  ('Cat officers plan', 'lead')):
            with self.subTest(title=title, visibility=visibility):
                body = 'Cat mirrors stay reserved for nine hours. Top five may hit bases 1-10.'
                general = KnowledgeSection('Family attacks', 'General family guidance.', file_name='family.md')
                section = KnowledgeSection(title, body, visibility, 'host-renamed-plan.md')
                catalog = KnowledgeCatalog('now', (general, section)).prepare_prompts()
                context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
                context.source_message.channel.name = 'bec-war-discussion'
                context.state.scope = EvidenceScope(91, frozenset(), frozenset())
                registry = {tool.definition.name: tool for tool in TOOLS
                            if tool.definition.name == 'read_community_reference'}
                requests = []
                final = ('BEC reserves mirrors for nine hours, with a top-five exception.'
                         if visibility == 'public' else "BEC's war instructions aren't available here.")

                async def provider(**options):
                    requests.append(options)
                    system = options['messages'][0]['content']
                    if system == ROUTE_INSTRUCTION:
                        return provider_response('research')
                    if system == answer_instructions(registry):
                        _, answer_context, evidence = writer_payload(options)
                        self.assertEqual(answer_context['current_channel']['name'], 'bec-war-discussion')
                        rows = evidence['policy']['rows']
                        self.assertEqual([row['body'] for row in rows],
                                         [body] if visibility == 'public' else [])
                        self.assertEqual(evidence['policy']['selection_complete'], visibility == 'public')
                        self.assertIn("If\nthat clan's rule was not read, say it is unavailable here", system)
                        return provider_response(final)
                    self.assertIn('bec-war-discussion', str(options['messages']))
                    self.assertIn('use current_channel, its parent/category', system)
                    return provider_response(calls=[call('submit_request_plan', _plan([
                        {'id': 'policy', 'capability': 'read_community_reference',
                         'arguments': {'reference_kind': 'knowledge', 'titles': [title]}, 'depends_on': []}]))])

                model = client(provider)
                try:
                    with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                        answer = await AgentService(model).answer(
                            question='When can my second attack go?', local_context='', context=context)
                    self.assertEqual(answer, final)
                    self.assertEqual(len(requests), 3)
                finally:
                    await model.close()


    async def _large_evidence_writer(self, clipped):
        requests = []
        rows = [{"member_id": index + 100, "name": f"Candidate {index}",
                 "attacks": index % 7, "opportunities": 7,
                 "note": "Evidence retained without interpreting source prose."}
                for index in range(180)]
        source = AsyncMock(return_value={"rows": rows})
        tool = RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
            {"type": "object", "properties": {}}), source)
        registry = {"read_sample": tool, "hidden": replace(tool, default_visible=False,
                    definition=AgentToolDefinition("hidden", "Hidden sample.", {}))}
        final = "Candidate 1 needs a check first: one attack used from seven opportunities."

        async def provider(**options):
            requests.append(deepcopy(options))
            system = options["messages"][0]["content"]
            if system == ROUTE_INSTRUCTION:
                return provider_response("research")
            if WRITER_INSTRUCTION in system:
                _, _, evidence = writer_payload(options)
                delivered = expand(evidence["sample"]["rows"])
                self.assertEqual(delivered, rows)
                self.assertLessEqual(options["max_tokens"], 16_000)
                # Reproduce the failure at the provider boundary: inherited
                # high/max thinking consumes the cap without visible output.
                if options["reasoning_effort"] != "low":
                    response = provider_response()
                    response.choices[0].finish_reason = "length"
                    response.usage.completion_tokens = options["max_tokens"]
                    return response
                response = provider_response(final)
                if clipped:
                    response.choices[0].finish_reason = "length"
                    response.usage.completion_tokens = options["max_tokens"]
                return response
            return provider_response(calls=[call("submit_request_plan", _plan([
                {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}],
                effort="max"))])

        model = client(provider)
        context = replace(_context(), deadline_monotonic=time.monotonic() + 180)
        try:
            with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="Which candidate needs a check first?",
                    local_context="", context=context)
            self.assertEqual(answer, final, [(item["reasoning_effort"], item.get("tool_choice")) for item in requests])
            self.assertEqual(len(requests), 4 if clipped else 3)
            self.assertEqual([item["reasoning_effort"] for item in requests],
                             ["low", "high", "low"] + (["low"] if clipped else []))
            source.assert_awaited_once()
        finally:
            await model.close()

    async def test_policy_subjects_and_background_provenance_reach_real_writer_intact(self):
        requests = []
        body = "Newly elected officers who have a badge must finish orientation. Visitors use the welcome desk."
        nearby = {"message_id": 75, "author_id": 52, "content": "My badge is blue."}
        raw = {"family": "read_community_reference", "selection": {"reference_kind": "knowledge"},
               "rows": [{"title": "Arrival", "body": body}]}
        source = AsyncMock(return_value=deepcopy(raw))
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
            {"type": "object", "properties": {}}), source)}
        context = _context()
        context.state.local_messages = [nearby]

        async def provider(**options):
            requests.append(options)
            system = options["messages"][0]["content"]
            if system == ROUTE_INSTRUCTION:
                return provider_response("research")
            if system == answer_instructions(registry):
                self.assertIn(WRITER_INSTRUCTION, system)
                _, answer_context, evidence = writer_payload(options)
                self.assertEqual(answer_context["asker"]["member_id"], 41)
                self.assertEqual(evidence["sample"]["rows"], [{"body": body}])
                self.assertEqual(evidence["sample"]["reference_kind"], "knowledge")
                self.assertEqual(evidence["sample"]["evidence_role"], "reference")
                self.assertEqual(evidence["local_context"]["messages"], [nearby])
                self.assertEqual(evidence["local_context"]["evidence_role"], "nearby_conversation")
                return provider_response("Visitors use the welcome desk.")
            return provider_response(calls=[call("submit_request_plan", _plan([
                {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])

        model = client(provider)
        try:
            with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="I have a badge. How does a visitor arrive?",
                    local_context=json.dumps(nearby), context=context)
            self.assertEqual(answer, "Visitors use the welcome desk.")
            self.assertEqual(len(requests), 3)
            self.assertEqual(raw["rows"][0]["body"], body)
        finally:
            await model.close()


    async def test_whole_request_goal_keeps_research_going_after_identity_lookup(self):
        requests, writers = [], []
        goal = "Show Lyra's regular war history."
        source = AsyncMock(side_effect=[{"rows": [{"member_id": 52}], "report_id": "sample:one", "next_offset": 1},
                                        {"rows": [{"member_id": 52, "war_type": "REG", "wars": 3, "wins": 2}]}])
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition(
            "read_sample", "Read sample facts.", {"type": "object", "properties": {
                "member_id": {"type": "integer"}, "report_id": {"type": "string"},
                "offset": {"type": "integer"}}}), source)}

        async def provider(**options):
            requests.append(deepcopy(options))
            system = options["messages"][0]["content"]
            if system == ROUTE_INSTRUCTION:
                return provider_response("research")
            plan_tool = next(tool["function"] for tool in options["tools"]
                             if tool["function"]["name"] == "submit_request_plan")
            self.assertEqual(plan_tool["description"],
                "Submit the goal of the asker's whole request, scope and ordered capability steps needed to answer.")
            self.assertEqual(plan_tool["parameters"]["properties"]["goal"], {
                "type": "string", "minLength": 1, "maxLength": 240,
                "description": "The outcome the asker wants from the whole request, not the current step. "
                               "State it the same way in every plan for this request.",
            })
            if system != answer_instructions(registry):
                source.assert_not_awaited()
                return provider_response(calls=[call("submit_request_plan", {**_plan([
                    {"id": "identity", "capability": "read_sample", "arguments": {}, "depends_on": []}]),
                    "goal": goal})])
            writers.append(options)
            prompt, _, evidence = writer_payload(options)
            self.assertEqual(prompt["request"], goal)
            self.assertEqual(prompt["asker_wants"], [goal] * len(writers))
            self.assertNotIn(goal, system)
            self.assertEqual(evidence["identity"]["rows"], [{"member_id": 52}])
            self.assertNotIn("report_id", evidence["identity"])
            self.assertNotIn("next_offset", evidence["identity"])
            self.assertEqual(prompt["read_handles"]["identity"], {"report_id": "sample:one", "next_offset": 1})
            self.assertFalse(any(message["role"] == "tool" for message in options["messages"]))
            if len(writers) == 1:
                source.assert_awaited_once()
                self.assertNotIn("war_history", evidence)
                return provider_response(calls=[call("submit_request_plan", {**_plan([
                    {"id": "war_history", "capability": "read_sample", "arguments": {
                        "member_id": evidence["identity"]["rows"][0]["member_id"],
                        "report_id": "sample:one", "offset": 1},
                     "depends_on": ["identity"]}]), "goal": goal})])
            self.assertEqual(evidence["war_history"]["rows"],
                             [{"member_id": 52, "war_type": "REG", "wars": 3, "wins": 2}])
            return provider_response("Lyra joined three regular wars and won two.")

        model = client(provider)
        try:
            with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question=goal,
                    local_context="", context=_context())
            self.assertEqual(answer, "Lyra joined three regular wars and won two.")
            self.assertEqual(len(requests), 4)
            self.assertEqual(len(writers), 2)
            self.assertEqual(writers[0]["messages"][0], writers[1]["messages"][0])
            self.assertEqual(source.await_count, 2)
            self.assertEqual(source.await_args_list[1].args[1],
                             {"member_id": 52, "report_id": "sample:one", "offset": 1})
        finally:
            await model.close()

    async def test_authorized_attachment_reaches_real_writer_on_research_and_family_routes(self):
        buffer = BytesIO()
        with Image.new("RGB", (3072, 1792), "navy") as screenshot:
            screenshot.save(buffer, format="PNG")
        png = buffer.getvalue()
        for family in (False, True):
            with self.subTest(family=family):
                context = _context()
                attachment = SimpleNamespace(content_type="image/png", filename="sample.png",
                                             size=len(png), read=AsyncMock(return_value=png))
                context.source_message.attachments = [attachment]
                context = replace(context, attachment_sources=(context.source_message,))
                requests, writers = [], []
                raw = {"rows": [{"name": "Vega", "signed_up_ts": 1738555506}]}
                source = AsyncMock(return_value=deepcopy(raw))
                tool = RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
                    {"type": "object", "properties": {}}), source)
                registry = {"read_sample": tool}
                if family:
                    registry["hidden"] = replace(tool, default_visible=False,
                        definition=AgentToolDefinition("hidden", "Hidden sample.", {}))

                async def provider(**options):
                    requests.append(deepcopy(options))
                    system = options["messages"][0]["content"]
                    if system == ROUTE_INSTRUCTION:
                        return provider_response("research")
                    parts = options["messages"][1]["content"]
                    self.assertIsInstance(parts, list)
                    image_parts = [part for part in parts if part["type"] == "image_url"]
                    self.assertEqual(len(image_parts), 1)
                    self.assertEqual(image_parts[0]["image_url"]["url"],
                                     "data:image/png;base64," + base64.b64encode(png).decode("ascii"))
                    self.assertEqual(image_parts[0]["image_url"]["detail"], "high")
                    self.assertIn(IMAGE_EVIDENCE_INSTRUCTION, system)
                    data = base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1])
                    with Image.open(BytesIO(data)) as delivered:
                        self.assertEqual(delivered.size, (3072, 1792))
                    if WRITER_INSTRUCTION in system:
                        writers.append(options)
                        prompt, _, evidence = writer_payload(options)
                        self.assertEqual(len(prompt["images"]), 1)
                        self.assertIn(WRITER_INSTRUCTION, system)
                        self.assertIn("message 71", prompt["images"][0])
                        self.assertEqual(evidence["sample"]["rows"], [
                            {"name": "Vega", "signed_up_ts": "2025-02-03 04:05:06 UTC"}])
                        self.assertEqual(context.state.image_sources, [(91, 71)])
                        self.assertIn(91, context.state.source_channels)
                        return provider_response("The screenshot and account agree.")
                    return provider_response(calls=[call("submit_request_plan", _plan([
                        {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])

                model = client(provider)
                try:
                    with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                        answer = await AgentService(model).answer(question="Compare this screenshot to the account.",
                            local_context="", context=context)
                    self.assertEqual(answer, "The screenshot and account agree.")
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(len(writers), 1)
                    attachment.read.assert_awaited_once()
                    self.assertEqual(raw["rows"][0]["signed_up_ts"], 1738555506)
                finally:
                    await model.close()

    async def test_real_writer_preserves_access_missing_input_and_failed_read_reasons(self):
        cases = (
            ({"error": "Lead access is required.", "required_access": ["lead"],
              "available_privately": False},
             {"status": "denied", "reader_roles": [[]], "available_privately": False}),
            ({"error": "This channel cannot disclose that material.", "cause": "access_denied",
              "available_privately": True},
             {"status": "denied", "available_privately": True}),
            ({"error": "TRANSPORT_DIAGNOSTIC: connection failed", "cause": "source_unavailable",
              "available_privately": False}, {"status": "unavailable", "available_privately": False}),
            ({"status": "needs_input", "missing_fields": ["season"], "missing": ["Which season?"],
              "choices": [{"name": "Spring", "season": "2026-04"}]},
             {"status": "needs_input", "missing_fields": ["season"], "missing": ["Which season?"],
              "choices": [{"name": "Spring", "season": "2026-04"}]}),
        )
        for raw, expected in cases:
            with self.subTest(status=expected["status"], private=expected.get("available_privately")):
                requests, writers = [], []
                source = AsyncMock(return_value=deepcopy(raw))
                registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition(
                    "read_sample", "Read sample facts.", {"type": "object", "properties": {
                        "clan_code": {"type": "string"}}}), source)}

                async def provider(**options):
                    requests.append(deepcopy(options))
                    system = options["messages"][0]["content"]
                    if system == ROUTE_INSTRUCTION:
                        return provider_response("research")
                    if system in (answer_instructions(), answer_instructions(registry)):
                        writers.append(options)
                        prompt, _, evidence = writer_payload(options)
                        self.assertEqual(prompt["asker_wants"], ["Answer from the selected data"])
                        limit = evidence["sample"]
                        for key, value in expected.items():
                            self.assertEqual(limit[key], value)
                        self.assertEqual(limit["subject"], {"clan_code": "C"})
                        if raw.get('error') and expected['status'] != 'denied':
                            self.assertEqual(limit['failed_read'], {
                                'what': 'read_sample', 'reason': raw['error']})
                        if expected["status"] == "denied":
                            self.assertNotIn('failed_read', limit)
                            self.assertNotIn(raw['error'], json.dumps(evidence))
                            constraints = json.dumps(writer_payload(options)[0])
                            self.assertIn("Stop that restricted part", constraints)
                        return provider_response("That detail needs clarification or permitted access.")
                    return provider_response(calls=[call("submit_request_plan", _plan([
                        {"id": "sample", "capability": "read_sample", "arguments": {"clan_code": "C"},
                         "depends_on": []}]))])

                model = client(provider)
                try:
                    with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                        await AgentService(model).answer(question="Check clan C's season.",
                            local_context="", context=_context())
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(len(writers), 1)
                finally:
                    await model.close()

    async def test_image_access_revoked_at_writer_creation_prevents_another_provider_call(self):
        context = _context()
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                               "QVR42mP8/x8AAwMCAO+jSmsAAAAASUVORK5CYII=")
        context.source_message.attachments = [SimpleNamespace(content_type="image/png", filename="sample.png",
            size=len(png), read=AsyncMock(return_value=png))]
        context = replace(context, attachment_sources=(context.source_message,))
        requests = []
        source = AsyncMock(return_value={"rows": [{"name": "Vega"}]})
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
            {"type": "object", "properties": {}}), source)}

        async def provider(**options):
            requests.append(deepcopy(options))
            if options["messages"][0]["content"] == ROUTE_INSTRUCTION:
                return provider_response("research")
            self.assertEqual(len(requests), 2, "Revoked images reached the writer provider.")
            return provider_response(calls=[call("submit_request_plan", _plan([
                {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])

        model = client(provider)
        create = model.create_agent_session

        def revoke_before_submission(**options):
            session = create(**options)
            if options["system_prompt"] in (answer_instructions(), answer_instructions(registry)):
                self.assertTrue(options["images"])
                context.member.roles = []
            return session

        model.create_agent_session = revoke_before_submission
        try:
            with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                with self.assertRaises(AgentAccessLost):
                    await AgentService(model).answer(question="Compare this screenshot to the account.",
                        local_context="", context=context)
            self.assertEqual(len(requests), 2)
            source.assert_awaited_once()
        finally:
            await model.close()

    async def test_real_writer_keeps_authorized_checkpoint_and_history_completeness(self):
        # Leave room between whole recent turns for the checkpoint even when
        # mandatory context includes the requester's role-permitted commands.
        turns = tuple(_turn(index, channel=91, payload_characters=0 if index <= 16 else 80_000)
                      for index in range(1, 41))
        turns = (replace(turns[0], record=replace(turns[0].record,
                 question="Use May 4 through May 10 for the comparison.")), *turns[1:])
        checkpoint = build_history_checkpoint(turns, covered_turn_count=16,
                                             created_at=datetime(2026, 9, 18, tzinfo=timezone.utc))
        context = replace(_context(), history=turns)
        context.state.authorized_checkpoint = checkpoint
        requests, writers, planner_context = [], [], []
        source = AsyncMock(return_value={"rows": [{"clan_code": "C", "name": "Cedar"}]})
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
            {"type": "object", "properties": {}}), source)}

        async def provider(**options):
            requests.append(deepcopy(options))
            system = options["messages"][0]["content"]
            if system == ROUTE_INSTRUCTION:
                return provider_response("research")
            if system in (answer_instructions(), answer_instructions(registry)):
                writers.append(options)
                _, answer_context, _ = writer_payload(options)
                original = planner_context[0]
                retained_checkpoint = original.split("<history_checkpoint>\n")[1].split("\n</history_checkpoint>")[0]
                status = json.loads(original.split("<history_status>\n")[1].split("\n</history_status>")[0])
                self.assertEqual(answer_context["history_checkpoint"], retained_checkpoint)
                self.assertEqual(retained_checkpoint, checkpoint.summary)
                self.assertIn("May 4 through May 10", retained_checkpoint)
                self.assertEqual(answer_context["history_status"], status)
                self.assertGreaterEqual(status["older_retained_turns_available"], 16)
                self.assertTrue(status["history_may_be_incomplete"])
                self.assertIn(91, context.state.source_channels)
                return provider_response("Cedar's comparison uses the agreed period.")
            planner_context.append(options["messages"][1]["content"])
            return provider_response(calls=[call("submit_request_plan", _plan([
                {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])

        model = client(provider)
        try:
            with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="Compare that period.", local_context="", context=context)
            self.assertEqual(answer, "Cedar's comparison uses the agreed period.")
            self.assertEqual(len(requests), 3)
            self.assertEqual(len(writers), 1)
        finally:
            await model.close()


class AnswerEvidenceTests(unittest.TestCase):

    def test_failed_flags_without_error_keep_failure_distinct_from_empty_records(self):
        projected = answer_evidence({'failed': {'flags': {'status': 'failed'},
            'cause': 'source_unavailable'}, 'empty': {'rows': []}})
        self.assertEqual(projected['failed']['failed_read'], {
            'what': 'failed', 'reason': 'source_unavailable'})
        self.assertEqual(projected['empty'], {'rows': []})

    def test_projection_never_edits_source_prose_or_command_notes(self):
        body = "selection after notes flags 73.42% — names stay exactly as supplied"
        raw = {"read": {"rows": [{"body": body, "notes": body, "selection": body}],
                        "notes": [body], "coverage_note": "planner only"},
               "retained": {"old": {"error": "failed attempt", "argument_schema": {"secret": True}},
                            "good": {"sections": [{"body": body}]}}}
        original = deepcopy(raw)
        projected = answer_evidence(raw)
        self.assertEqual(projected["read"]["rows"], raw["read"]["rows"])
        self.assertEqual(projected["read"]["notes"], [body])
        self.assertEqual(projected["retained"], {"old": {"status": "unavailable",
            "failed_read": {"what": "old", "reason": "failed attempt"}},
                                               "good": {"sections": [{"body": body}]}})
        self.assertEqual(raw, original)


    def test_navigation_cannot_reenter_through_retained_reads_or_clash_item_coverage(self):
        raw = {"discovery": {"catalogue": "transport signatures", "total_capabilities": 10},
               "retained": {"discovery": {"catalogue": "old signatures", "total_capabilities": 4},
                            "log": {"source": "clash_api", "items": [{"data": {"stars": 2},
                                "coverage": {"requested_after": "implicit start", "history_complete": False,
                                             "returned_range": {"oldest": "2026-06-10"}}}]}}}
        projected = answer_evidence(raw)
        self.assertNotIn("discovery", projected)
        self.assertNotIn("discovery", projected["retained"])
        coverage = projected["retained"]["log"]["items"][0]["coverage"]
        self.assertEqual(coverage, {"history_complete": False,
                                    "returned_range": {"oldest": "2026-06-10"}})
