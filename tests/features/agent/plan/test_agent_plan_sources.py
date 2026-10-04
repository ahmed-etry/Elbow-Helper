"""Explicit source identifiers stay separate across entity kinds."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.plan.sources import (
    named_sources, requested_channels, requested_clans, requested_member_ids, requested_player_tags,
)
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


class NamedSourceTests(unittest.TestCase):
    def test_channel_mentions_and_exact_names_resolve_only_matching_channels(self):
        channels = [SimpleNamespace(id=101, name="synthetic-name"),
                    SimpleNamespace(id=202, name="other-name")]
        self.assertEqual(requested_channels("<#101> #SYNTHETIC-NAME", channels), frozenset({101}))
        self.assertEqual(requested_channels("unrelated words", channels), frozenset())

    def test_member_mentions_do_not_become_channel_sources(self):
        self.assertEqual(requested_member_ids("<@101> <@!202> <#303>"), frozenset({101, 202}))
        self.assertEqual(requested_channels("<@101> <@!202>", ()), frozenset())

    def test_every_configured_clan_identifier_stays_out_of_account_sources(self):
        for clan in CLANS.values():
            with self.subTest(code=clan.code):
                self.assertEqual(requested_clans(clan.code), frozenset({clan.code}))
                self.assertEqual(requested_clans(clan.code.lower()), frozenset())
                self.assertEqual(requested_clans(clan.tag), frozenset({clan.code}))
                self.assertEqual(requested_player_tags(clan.tag), frozenset())

    def test_account_tags_are_normalized_without_matching_channel_names(self):
        self.assertEqual(requested_player_tags("#p0 #P0"), frozenset({"#P0"}))
        self.assertEqual(requested_player_tags("#synthetic-name"), frozenset())

    def test_clan_mentions_do_not_limit_sources_but_channels_and_threads_do(self):
        clans = {"SYN": SimpleNamespace(code="SYN", tag="#Q0"),
                 "ALT": SimpleNamespace(code="ALT", tag="#Q2")}
        channels = [SimpleNamespace(id=101, name="synthetic-channel"),
                    SimpleNamespace(id=202, name="synthetic-thread")]
        with (patch("elbow_helper.features.agent.plan.sources.CLANS", clans),
              patch("elbow_helper.features.agent.plan.sources.CLAN_ORDER", tuple(clans))):
            for text in ("SYN role", "#Q0 role"):
                with self.subTest(text=text):
                    sources = named_sources(text + " <#101> #synthetic-thread", channels)
                    self.assertNotIn("clan", sources)
                    self.assertEqual(sources["clash_account"], frozenset())
                    self.assertEqual(sources["discord_channel"], frozenset({101, 202}))


class NamedClanReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_clan_read_passes_and_runs_when_a_role_mentions_a_clan_code(self):
        read = AsyncMock(return_value={"clan_code": "ALT"})
        tool = RegisteredAgentTool(AgentToolDefinition("read_synthetic_clan", "Read synthetic clan data.", {
            "type": "object", "properties": {"clan_code": {"type": "string", "enum": ["SYN", "ALT"]}},
            "required": ["clan_code"], "additionalProperties": False,
        }), read, contract=CapabilityContract((("clan_code", "clan"),), ()))
        registry = {tool.definition.name: tool}
        plan = _plan([{"id": "other_clan", "capability": tool.definition.name,
                       "arguments": {"clan_code": "ALT"}, "reason": "Check synthetic linked accounts",
                       "depends_on": []}])
        clans = {"SYN": SimpleNamespace(code="SYN", tag="#Q0"),
                 "ALT": SimpleNamespace(code="ALT", tag="#Q2")}
        session = _Session([_model_step(plan), AgentStep("Synthetic clan answer", (), AgentUsage())], [])
        with (patch("elbow_helper.features.agent.plan.sources.CLANS", clans),
              patch("elbow_helper.features.agent.plan.sources.CLAN_ORDER", tuple(clans)),
              patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry)):
            sources = named_sources("Inspect accounts of the SYN role", ())
            self.assertTrue(check_plan(plan, registry, sources).ok)
            answer = await AgentService(_Model(session)).answer(
                question="Inspect accounts of the SYN role", local_context="", context=_context(),
            )
        self.assertEqual(answer, "Synthetic clan answer")
        read.assert_awaited_once()
        self.assertEqual(read.await_args.args[1], {"clan_code": "ALT"})

    def test_named_channel_still_refuses_a_different_channel(self):
        tool = RegisteredAgentTool(AgentToolDefinition("read_synthetic_channel", "Read synthetic channel data.", {
            "type": "object", "properties": {"channel_id": {"type": "integer", "minimum": 1}},
            "required": ["channel_id"], "additionalProperties": False,
        }), AsyncMock(), contract=CapabilityContract((("channel_id", "discord_channel"),), ()))
        plan = _plan([{"id": "channel", "capability": tool.definition.name, "arguments": {"channel_id": 202},
                       "reason": "Inspect synthetic channel", "depends_on": []}])
        checked = check_plan(plan, {tool.definition.name: tool}, named_sources("Read <#101>", ()))
        self.assertFalse(checked.ok)
        self.assertEqual(checked.offered, ("202",))
        plan["steps"][0]["arguments"]["channel_id"] = 101
        self.assertTrue(check_plan(plan, {tool.definition.name: tool}, named_sources("Read <#101>", ())).ok)
