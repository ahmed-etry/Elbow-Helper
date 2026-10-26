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

from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
from elbow_helper.features.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.social_reply import MEME_SEND_FIT
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.conversation.context import build_history_checkpoint
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.features.agent.plan.format import (
    IMAGE_EVIDENCE_INSTRUCTION, WRITER_INSTRUCTION, answer_instructions, system_instructions,
)
from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.agent.prompts import ANSWER_RULES, BANTER_RULES, DISCORD_REPLY_RULES
from elbow_helper.infrastructure.ai import AgentToolDefinition, DeepSeekTextClient
from features.agent.engine.test_agent_plan_flow import _context, _plan
from features.agent.engine.test_writer_tables import expand
from features.agent.conversation.test_agent_context import _turn


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
        from elbow_helper.features.agent.families.reference_reads import TOOLS
        from elbow_helper.features.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
        from elbow_helper.features.agent.models import EvidenceScope

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
                        self.assertIn('otherwise say the clan rule is unavailable here', system)
                        return provider_response(final)
                    self.assertIn('bec-war-discussion', str(options['messages']))
                    self.assertIn('use current_channel, its parent/category', system)
                    return provider_response(calls=[call('submit_request_plan', _plan([
                        {'id': 'policy', 'capability': 'read_community_reference',
                         'arguments': {'reference_kind': 'knowledge', 'titles': [title]}, 'depends_on': []}]))])

                model = client(provider)
                try:
                    with patch('elbow_helper.features.agent.engine.service.build_agent_tools', return_value=registry):
                        answer = await AgentService(model).answer(
                            question='When can my second attack go?', local_context='', context=context)
                    self.assertEqual(answer, final)
                    self.assertEqual(len(requests), 3)
                finally:
                    await model.close()

    async def test_social_media_keeps_planned_text_only_as_fallback_without_forced_rewrite(self):
        for outcome in ('no_fit', 'missing', 'selected', 'optional'):
            with self.subTest(outcome=outcome):
                context = _context()
                context.source_message.add_reaction = AsyncMock()
                context.state.channel_bot_gifs = (True,) if outcome == 'optional' else ()
                requests = []
                selected = outcome in ('selected', 'optional')
                url = 'https://giphy.com/gifs/synthetic-media'
                media = 'missing.gif' if outcome == 'missing' else url
                requested = outcome not in ('no_fit', 'optional')
                final = 'Synthetic initial draft.'

                async def provider(**options):
                    requests.append(deepcopy(options))
                    if options['messages'][0]['content'] == ROUTE_INSTRUCTION:
                        return provider_response('social')
                    feedback = [json.loads(message['content']) for message in options['messages']
                                if isinstance(message, dict) and message['role'] == 'tool']
                    if feedback:
                        return provider_response(final)
                    return provider_response(calls=[call('submit_social_reply', {
                        'mechanic_check': 'no_gameplay_number', 'reply_form': 'text_reactions',
                        'reaction_fit': True, 'reaction_basis': 'The message requests a celebration.',
                        'text': 'Synthetic initial draft.', 'gif_requested': requested,
                        'meme_shortlist': [{'id': item, 'fit': MEME_SEND_FIT + (outcome == 'optional') if item == 'ts_lowkey_tuff' and outcome != 'no_fit' else 1} for item in
                            ('ts_lowkey_tuff', 'things_will_happen_horse', 'son_not_tuff')],
                        'gif_with_text': True, 'reactions': ['\U0001f680'],
                    })])

                model = client(provider)
                try:
                    with patch.dict(ITEM_BY_ID, ts_lowkey_tuff={**ITEM_BY_ID['ts_lowkey_tuff'], 'media': media}):
                        answer = await AgentService(model).answer(question='Show a synthetic meme.',
                            local_context='', context=context)
                    self.assertEqual(answer, '' if selected else final)
                    if selected:
                        self.assertEqual(context.state.gif_fallback_text, final)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(context.state.gif_url, url if selected else None)
                    self.assertEqual(context.state.reactions.emojis, ['\U0001f680'])
                    context.source_message.add_reaction.assert_awaited_once_with('\U0001f680')
                    surface = AgentDeliveryMixin()
                    surface._send_response = AsyncMock()
                    surface._send_gif = AsyncMock()
                    with patch('elbow_helper.features.agent.delivery.assert_answer_scope', AsyncMock()):
                        await surface.send_response(context.source_message, answer, None, context=context)
                    self.assertEqual(surface._send_response.await_count, int(not selected))
                    if not selected:
                        self.assertEqual(surface._send_response.await_args.args[1], final)
                    self.assertEqual(surface._send_gif.await_count, int(selected))
                finally:
                    await model.close()

    async def test_large_evidence_max_plan_delivers_within_family_request_budget(self):
        for clipped in (False, True):
            with self.subTest(output_limit_reached=clipped):
                await self._large_evidence_writer(clipped)

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
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
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
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="I have a badge. How does a visitor arrive?",
                    local_context=json.dumps(nearby), context=context)
            self.assertEqual(answer, "Visitors use the welcome desk.")
            self.assertEqual(len(requests), 3)
            self.assertEqual(raw["rows"][0]["body"], body)
        finally:
            await model.close()

    async def test_canonical_public_help_reaches_research_and_expanded_social_writer(self):
        expected = [{"path": entry.path, "summary": entry.summary}
                    for entry in HELP_ENTRIES if entry.visible_to is None]
        for social in (False, True):
            with self.subTest(social=social):
                requests = []
                source = AsyncMock(return_value={"rows": []})
                registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
                    {"type": "object", "properties": {}}), source)}

                async def provider(**options):
                    requests.append(options)
                    system = options["messages"][0]["content"]
                    if system == ROUTE_INSTRUCTION:
                        return provider_response("social" if social else "research")
                    if system == answer_instructions(registry):
                        _, answer_context, _ = writer_payload(options)
                        self.assertEqual(answer_context["bot_command_overview"]["commands"], expected)
                        return provider_response("I help with clan tasks.")
                    prompt = '\n'.join(message['content'] for message in options['messages']
                                       if isinstance(message, dict) and isinstance(message.get('content'), str))
                    if social and '<bot_command_overview>' not in prompt:
                        return provider_response(calls=[call('load_research_context', {})])
                    overview = prompt.split("<bot_command_overview>\n")[1].split("\n</bot_command_overview>")[0]
                    self.assertEqual(json.loads(overview.split("\n", 1)[1])["commands"], expected)
                    return provider_response(calls=[call("submit_request_plan", _plan([
                        {"id": "sample", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])

                model = client(provider)
                try:
                    with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                        answer = await AgentService(model).answer(question="Describe your purpose.",
                            local_context="", context=_context())
                    self.assertEqual(answer, "I help with clan tasks.")
                    self.assertEqual(len(requests), 4 if social else 3)
                finally:
                    await model.close()

    async def test_real_writer_receives_facts_without_planning_context_on_each_route(self):
        for route in ("research", "family", "expanded", "recovery"):
            with self.subTest(route=route):
                requests, writers = [], []
                question = ("Give Orion's raid average for April 1 through April 8."
                            if route == "family" else "Give Orion's raid average.")
                raw = {
                    "rows": [{"player_name": "Orion", "average": None, "observed_range": {
                        "oldest": "2026-04-06", "newest": "2026-04-08"}}],
                    "sections": [{"title": "Arrival", "body":
                        "Visitors use the welcome desk. Members use the transfer desk."}],
                    "history_coverage": {"status": "partial", "requested_after": "2026-01-19",
                        "message": "INTERNAL_COVERAGE", "reasons": ["INTERNAL_REASON"]},
                    "selection": {"after": "2026-01-19"},
                    "coverage_note": "INTERNAL_NOTE", "detail_projection": "INTERNAL_PROJECTION",
                    "collection_limits": ["INTERNAL_POLLING"], "source_complete": False,
                    "flags": {"status": "partial", "truncated": False},
                }
                source = AsyncMock(return_value=deepcopy(raw))
                tool = RegisteredAgentTool(AgentToolDefinition("read_sample", "Read sample facts.",
                    {"type": "object", "properties": {}}), source)
                registry = {"read_sample": tool}
                if route == "family":
                    registry["hidden"] = replace(tool, default_visible=False,
                        definition=AgentToolDefinition("hidden", "Hidden sample.", {}))
                research_rounds = 0

                async def provider(**options):
                    nonlocal research_rounds
                    requests.append(deepcopy(options))
                    system = options["messages"][0]["content"]
                    if system == ROUTE_INSTRUCTION:
                        return provider_response("social" if route == "expanded" else "research")
                    if WRITER_INSTRUCTION in system:
                        writers.append(options)
                        self.assertIn(WRITER_INSTRUCTION, system)
                        self.assertEqual(options.get("tool_choice") == "none", route == "recovery")
                        payload = options["messages"][1]["content"]
                        context_json, evidence_json = payload.split("\nAvailable evidence:\n", 1)
                        context = json.loads(json.loads(context_json)["context"])
                        self.assertEqual(context["request"], question)
                        self.assertEqual(context["nearby_conversation"], "Nearby member text.")
                        self.assertEqual(context["conversation_history"], "Earlier subject: Orion.")
                        evidence = json.loads(evidence_json)["results"]
                        if route != "recovery":
                            self.assertEqual(evidence["sample"]["rows"], raw["rows"])
                            self.assertEqual(evidence["sample"]["sections"], raw["sections"])
                            self.assertFalse(evidence["sample"]["source_complete"])
                        serialized = json.dumps(options, ensure_ascii=False)
                        for marker in ("INTERNAL_", "CATALOGUE_LOCATOR", "PLAN_GOAL", "DATA_LOCATOR"):
                            self.assertNotIn(marker, serialized)
                        if route != "recovery":
                            self.assertEqual(evidence["sample"]["history_coverage"]["requested_after"],
                                             "2026-01-19")
                        self.assertNotIn(BANTER_RULES, system)
                        self.assertIn(ANSWER_RULES, system)
                        self.assertIn(DISCORD_REPLY_RULES, system)
                        return provider_response("Orion's raid average is unconfirmed.")
                    tools = {tool["function"]["name"] for tool in options.get("tools", ())}
                    if "submit_social_reply" in tools and research_rounds == 0:
                        research_rounds += 1
                        return provider_response(calls=[call("load_research_context", {})])
                    if route == "recovery":
                        return provider_response(calls=[call("unknown", {})])
                    if not any(isinstance(message, dict) and message["role"] == "tool"
                               and '"sample"' in message["content"]
                               for message in options["messages"]):
                        plan = _plan([{"id": "sample", "capability": "read_sample",
                                       "arguments": {}, "depends_on": []}])
                        plan["goal"] = "PLAN_GOAL"
                        return provider_response(calls=[call("submit_request_plan", plan)])
                    self.fail("A draft-producing provider round preceded the isolated writer.")

                model = client(provider)
                guide = SimpleNamespace(for_levels=lambda _: "DATA_LOCATOR")
                try:
                    with (patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                                return_value=registry),
                          patch("elbow_helper.features.agent.engine.service.scoped_channel_directory",
                                AsyncMock(return_value="CATALOGUE_LOCATOR"))):
                        answer = await AgentService(model, data_guide=guide).answer(
                            question=question, local_context="Nearby member text.",
                            conversation_history="Earlier subject: Orion.", context=_context())
                    self.assertEqual(answer, "Orion's raid average is unconfirmed.")
                    self.assertEqual(len(writers), 1)
                    self.assertEqual(len(requests), 4 if route in ("expanded", "recovery") else 3)
                    self.assertTrue(any("CATALOGUE_LOCATOR" in json.dumps(item, default=vars) for item in requests))
                    self.assertEqual(source.await_count, 0 if route == "recovery" else 1)
                finally:
                    await model.close()

    async def test_real_writer_can_read_a_remaining_gap_without_a_draft_round(self):
        requests, writers = [], []
        source = AsyncMock(side_effect=[{"rows": [{"member_id": 52}], "report_id": "sample:one", "next_offset": 1},
                                        {"rows": [{"member_id": 52, "player_name": "Vega"}]}])
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition(
            "read_sample", "Read sample facts.", {"type": "object", "properties": {
                "member_id": {"type": "integer"}, "report_id": {"type": "string"},
                "offset": {"type": "integer"}}}), source)}

        async def provider(**options):
            requests.append(deepcopy(options))
            system = options["messages"][0]["content"]
            if system == ROUTE_INSTRUCTION:
                return provider_response("research")
            if system != answer_instructions(registry):
                return provider_response(calls=[call("submit_request_plan", _plan([
                    {"id": "identity", "capability": "read_sample", "arguments": {}, "depends_on": []}]))])
            writers.append(options)
            prompt, _, evidence = writer_payload(options)
            self.assertEqual(evidence["identity"]["rows"], [{"member_id": 52}])
            self.assertNotIn("report_id", evidence["identity"])
            self.assertNotIn("next_offset", evidence["identity"])
            self.assertEqual(prompt["read_handles"]["identity"], {"report_id": "sample:one", "next_offset": 1})
            self.assertFalse(any(message["role"] == "tool" for message in options["messages"]))
            if len(writers) == 1:
                return provider_response(calls=[call("submit_request_plan", _plan([
                    {"id": "account", "capability": "read_sample", "arguments": {
                        "member_id": 52, "report_id": "sample:one", "offset": 1},
                     "depends_on": []}]))])
            self.assertEqual(evidence["account"]["rows"], [{"member_id": 52, "player_name": "Vega"}])
            return provider_response("Vega is linked to that member.")

        model = client(provider)
        try:
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="Find the linked player.",
                    local_context="", context=_context())
            self.assertEqual(answer, "Vega is linked to that member.")
            self.assertEqual(len(requests), 4)
            self.assertEqual(len(writers), 2)
            self.assertEqual(source.await_count, 2)
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
                    with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                        answer = await AgentService(model).answer(question="Compare this screenshot to the account.",
                            local_context="", context=context)
                    self.assertEqual(answer, "The screenshot and account agree.")
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(len(writers), 1)
                    attachment.read.assert_awaited_once()
                    self.assertEqual(raw["rows"][0]["signed_up_ts"], 1738555506)
                finally:
                    await model.close()

    async def test_real_writer_preserves_access_and_missing_input_without_transport_errors(self):
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
                        _, _, evidence = writer_payload(options)
                        limit = evidence["sample"]
                        for key, value in expected.items():
                            self.assertEqual(limit[key], value)
                        self.assertEqual(limit["subject"], {"clan_code": "C"})
                        self.assertNotIn("TRANSPORT_DIAGNOSTIC", json.dumps(options))
                        if expected["status"] == "denied":
                            constraints = json.dumps(writer_payload(options)[0])
                            self.assertIn("Stop that restricted part", constraints)
                        return provider_response("That detail needs clarification or permitted access.")
                    return provider_response(calls=[call("submit_request_plan", _plan([
                        {"id": "sample", "capability": "read_sample", "arguments": {"clan_code": "C"},
                         "depends_on": []}]))])

                model = client(provider)
                try:
                    with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
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
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
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
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                answer = await AgentService(model).answer(question="Compare that period.", local_context="", context=context)
            self.assertEqual(answer, "Cedar's comparison uses the agreed period.")
            self.assertEqual(len(requests), 3)
            self.assertEqual(len(writers), 1)
        finally:
            await model.close()

    async def test_social_contract_checks_mechanic_before_text_through_real_provider(self):
        received = []

        async def provider(**options):
            received.append(options)
            if options["messages"][0]["content"] == ROUTE_INSTRUCTION:
                return provider_response("social")
            system = options["messages"][0]["content"]
            self.assertIn(BANTER_RULES, system)
            definition = next(tool["function"] for tool in options["tools"]
                              if tool["function"]["name"] == "submit_social_reply")
            schema = definition["parameters"]
            self.assertEqual(next(iter(schema["properties"])), "mechanic_check")
            self.assertIn("mechanic_check", schema["required"])
            self.assertEqual(schema["properties"]["mechanic_check"]["enum"],
                             ["no_gameplay_number", "possible", "unclear", "impossible"])
            return provider_response(calls=[call("submit_social_reply", {
                "mechanic_check": "impossible", "reply_form": "text", "text": "Check that score again.",
                "reaction_fit": False, "reaction_basis": "",
                "gif_requested": False,
                "meme_shortlist": [{"id": item, "fit": 1} for item in
                    ("ts_lowkey_tuff", "things_will_happen_horse", "son_not_tuff")],
                "reactions": []})])

        model = client(provider)
        try:
            with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value={}):
                answer = await AgentService(model).answer(question="My attack got 104% destruction, roast it.",
                    local_context="", context=_context())
            self.assertEqual(answer, "Check that score again.")
            self.assertEqual(len(received), 2)
        finally:
            await model.close()


class AnswerEvidenceTests(unittest.TestCase):
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
        self.assertEqual(projected["retained"], {"old": {"status": "unavailable"},
                                               "good": {"sections": [{"body": body}]}})
        self.assertEqual(raw, original)

    def test_research_and_writer_do_not_receive_the_banter_contract(self):
        for instructions in (system_instructions({}), system_instructions({}, actions_enabled=False),
                             answer_instructions()):
            self.assertNotIn(BANTER_RULES, instructions)
            self.assertIn(ANSWER_RULES, instructions)

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
