"""Crude banter reaches the social route with contextual limits."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from elbow_helper.features.agent.engine.routing import ROUTE_INSTRUCTION, SOCIAL_INSTRUCTION
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.social_reply import SOCIAL_REPLY_INSTRUCTION
from elbow_helper.features.agent.plan.format import system_instructions
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage, DeepSeekTextClient
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class PersonaSlangTests(unittest.IsolatedAsyncioTestCase):
    async def test_invited_roast_keeps_contextual_slang_on_both_routes(self):
        question = "I pinned the empty signup post. Tear into that."
        reply = "You pinned fuck-all. Outstanding."
        for route in ("social", "research"):
            with self.subTest(route=route):
                step = (social_step(reply, meme_id=None, self_target=True)
                        if route == "social" else AgentStep(reply, (), AgentUsage()))
                session = _AgentSession([step])
                model = _AgentModel(session, route=route)
                answer = await AgentService(model).answer(
                    question=question, local_context="Member invites a roast of their own message.",
                    context=_context(),
                )
                self.assertEqual(answer, reply)
                self.assertIn(question, model.request["prompt"])
                self.assertEqual(len(session.calls), 1)
                if route == "social":
                    self.assert_roast_policy(model.request["system_prompt"])
                else:
                    self.assertNotIn('Banter is crude by default', model.request["system_prompt"])

    def test_action_and_read_only_prompts_keep_the_same_roast_boundary(self):
        for actions in (False, True):
            with self.subTest(actions=actions):
                self.assert_roast_policy(system_instructions({}, actions_enabled=actions, social=True))

    def assert_roast_policy(self, prompt):
        self.assertIn("Banter is crude by default: harder roasts, dark humor, shared memes "
                      "and crude wordplay are normal here.", prompt)
        self.assertIn("A roast request gets a sharp roast at the requested intensity, "
                      "including when aimed at another member.", prompt)
        self.assertIn("Use crude insults directly when they fit, without an apology or a lecture.", prompt)
        self.assertIn("Never correct, censor or lecture members about their language.", prompt)
        self.assertNotIn("protected groups", prompt)
        self.assertNotIn("When banter or a roast is invited", prompt)
        self.assertNotIn("belong when invited by the exchange", prompt)
        self.assertNotIn("In an invited roast", prompt)
        self.assertNotIn("Dark in-joke roasting is welcome when invited and mutual", prompt)
        self.assertIn("Stop when someone is uncomfortable. Text, fitting reactions and the available GIFs", prompt)
        self.assertIn("never use leadership-only records for a joke", prompt)

    async def test_provider_payload_keeps_roast_contract_through_identity_and_text_repair(self):
        """Check real serialization and delivery, not a scripted model's sense of humor."""
        for repair in (False, True):
            with self.subTest(repair=repair):
                requests = []
                line = "Sample Member, even your introduction needs fucking supervision."
                person = SimpleNamespace(
                    id=701, name="sample.user", display_name="Sample Member",
                    global_name="Sample Member", nick="Sample Member", aliases=(),
                    mention="<@701>", roles=[], bot=False,
                )
                context = _context()
                context.guild.members = [context.member, person]
                context.guild.get_member = {member.id: member for member in context.guild.members}.get
                social_rounds = 0

                async def provider(**options):
                    nonlocal social_rounds
                    requests.append(deepcopy(options))
                    calls = []
                    if options["messages"][0]["content"] == ROUTE_INSTRUCTION:
                        content = "social"
                    else:
                        social_rounds += 1
                        if social_rounds == 1:
                            step = social_step("Draft.", meme_id=None, member_query="Sample Member",
                                               address_member=True)
                        elif repair:
                            step = AgentStep("", (AgentToolCall("bad", "submit_social_reply", "{"),), AgentUsage())
                        else:
                            step = social_step(line, meme_id=None, member_query="Sample Member")
                        if social_rounds == 3:
                            content = "<@701> " + line
                        else:
                            content = step.content
                            calls = [{"id": f"social-{social_rounds}", "type": "function", "function": {
                                "name": call.name, "arguments": call.arguments,
                            }} for call in step.tool_calls]
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(
                            role="assistant", content=content, tool_calls=calls), finish_reason="stop")],
                        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                              prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100),
                        id="synthetic-roast", model="synthetic-provider",
                    )

                transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=AsyncMock(side_effect=provider))), close=AsyncMock())
                transport.with_options = lambda **_: transport
                model = DeepSeekTextClient(None)
                model._client = transport
                try:
                    answer = await AgentService(model).answer(
                        question="Give Sample Member a vicious welcome.", local_context="", context=context,
                    )
                finally:
                    await model.close()
                self.assertEqual(answer, "<@701> " + line)
                self.assertEqual(context.state.resolved_members, {701: "Sample Member"})
                self.assertEqual(len(requests), 4 if repair else 3)
                self.assertIn("Writing or delivering a roast, tease or welcome also stays social",
                              requests[0]["messages"][0]["content"])
                self.assertIn("requests to change banter's tone, intensity or length stay social",
                              requests[0]["messages"][0]["content"])
                for request in requests[1:]:
                    self.assert_roast_policy(request["messages"][0]["content"])
                    self.assertIn(SOCIAL_INSTRUCTION, request["messages"][0]["content"])
                    self.assertIn(SOCIAL_REPLY_INSTRUCTION, request["messages"][0]["content"])
                    self.assertIn("Writing or delivering a roast, tease or welcome from supplied context stays here",
                                  request["messages"][0]["content"])
                    self.assertEqual(request["reasoning_effort"], "low")
                    self.assertNotIn("temperature", request)
                for request in requests[1:3]:
                    schema = next(tool['function']['parameters'] for tool in request['tools']
                                  if tool['function']['name'] == 'submit_social_reply')
                    self.assertIn("requested intensity", schema["properties"]["text"]["description"])
                    self.assertIn("Do not invent a target's real history, habits or gameplay", schema["properties"]["text"]["description"])
                if repair:
                    self.assertEqual(requests[-1]["tool_choice"], "none")
                self.assertEqual(context.state.reactions.emojis, [])
                self.assertIsNone(context.state.gif_url)
