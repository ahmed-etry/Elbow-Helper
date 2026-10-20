"""Anonymous conversations exercise real planning, tools, model views and delivery."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


from elbow_helper.configuration.roles import LEAD
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.engine.factual_answers import narrow_answer, clan_count_tag
from elbow_helper.features.agent.engine.grounding import GroundingError
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


def planned(identity, capability, arguments, *, reply=None):
    plan = _plan([{"id": identity, "capability": capability,
                   "arguments": arguments, "depends_on": []}])
    if reply is not None:
        plan["reply"] = reply
    return replace(_model_step(plan), usage=AgentUsage(1000, 100))


def unpack(items):
    return [dict(zip(items["columns"], row)) for row in items["rows"]] if isinstance(items, dict) else items


class RepairConversationTests(unittest.IsolatedAsyncioTestCase):
    def context(self, fetch):
        context = _context()
        context.member.roles.append(SimpleNamespace(id=next(iter(LEAD))))
        context.guild.members = [context.member]
        context.guild.chunked = True
        context.source_message.archive_reply = False
        context.source_message.mentions = ()
        context.source_message.guild = context.guild
        context.source_message.author = context.member
        context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=501))
        return replace(context, bot=SimpleNamespace(
            user=SimpleNamespace(id=999), clash_client=SimpleNamespace(configured=True, get=AsyncMock(side_effect=fetch)),
        ))

    async def test_original_count_recovers_from_404_with_reused_or_new_step_id(self):
        async def fetch(path, **options):
            return SimpleNamespace(ok=path.endswith("%23Q0"), status=200 if path.endswith("%23Q0") else 404,
                                   payload={"tag": "#Q0", "name": "Example clan", "members": 50})
        for corrected_id in ("clan", "resolved_clan"):
            with self.subTest(corrected_id=corrected_id):
                context = self.context(fetch)
                session = _Session([
                    planned("clan", "read_clash", {"kind": "clan", "tags": ["#P0"]}),
                    planned(corrected_id, "read_clash", {"kind": "clan", "tags": ["#Q0"]}),
                    AgentStep("49, full!", (), AgentUsage(1000, 100)),
                ], [])
                answer = await AgentService(_Model(session)).answer(question="Member count?", local_context="", context=context)
                self.assertEqual(answer, "50")
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(context.bot.clash_client.get.await_count, 2)
                delivery = AgentDeliveryMixin()
                delivery.bot = context.bot
                await delivery.send_response(context.source_message, answer, None, context=context)
                self.assertEqual(context.source_message.reply.await_args.args[0], "50")

    async def test_explicit_single_clan_count_uses_one_read_and_no_model(self):
        context = self.context(lambda *_args, **_options: SimpleNamespace(
            ok=True, status=200, payload={"tag": "#Q0", "members": 50, "memberList": []}))
        model = SimpleNamespace(create_agent_session=Mock(side_effect=AssertionError("Unneeded model")))
        answer = await AgentService(model).answer(question="How many members in #Q0 currently?", local_context="", context=context)
        self.assertEqual(answer, "50")
        context.bot.clash_client.get.assert_awaited_once()
        model.create_agent_session.assert_not_called()
        for question in ("How many members in #Q0 have 300 trophies?", "How many members in #Q0 last month?",
                         "How many members in #Q0 and #P0?", "Should we raise the member count for #Q0?"):
            self.assertIsNone(clan_count_tag(question))

    def test_count_subject_and_successful_conflicts(self):
        def observation(tag, value):
            return {"source": "clash_api", "kind": "clan", "items": [{"tag": tag, "status": "ok", "data": {"members": value}}]}
        good = observation("#Q0", 50)
        self.assertEqual(narrow_answer("How many members in #Q0?", {"one": good, "duplicate": good,
                                                                     "unrelated": observation("#P0", 40)}), "50")
        with self.assertRaises(GroundingError):
            narrow_answer("How many members in #Q0?", {"one": good, "conflict": observation("#Q0", 49)})
        named = observation("#Q0", 50)
        named["items"][0]["data"]["name"] = "Example clan"
        self.assertEqual(narrow_answer("How many members in Example clan?", {"one": named}), "50")
        with self.assertRaises(GroundingError):
            narrow_answer("How many members in Different clan?", {"one": named})
