"""All supplied identity hints must identify the same Discord member."""

from types import SimpleNamespace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.research.members import find_discord_members
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


def member(identifier, username, *, nickname=None, aliases=()):
    return SimpleNamespace(
        id=identifier, display_name=nickname or "Ahmad", global_name="Ahmad",
        name=username, nick=nickname, aliases=aliases, mention=f"<@{identifier}>",
        roles=[], bot=False,
    )


def context():
    result = _context()
    owner = member(42, "owner")
    owner.roles = result.member.roles
    for role in owner.roles:
        role.name = "Core"
        role.is_default = lambda: False
    other = member(101, "amaj", nickname="Ahmad")
    result.guild.members = [owner, other]
    result.guild.get_member = {item.id: item for item in result.guild.members}.get
    result.source_message.id = 300
    return result


class MemberIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_account_label_beats_partial_names_and_test_identities_are_excluded(self):
        from dataclasses import replace
        from elbow_helper.configuration.guild import TEST_ACCOUNT_IDS
        target = context()
        synthetic_test = member(next(iter(TEST_ACCOUNT_IDS)), 'amaj', nickname='Ahmad')
        target.guild.members.append(synthetic_test)
        links = Mock()
        links.get_links_for_members.return_value = {
            42: [{'player_name_last_seen': 'Moon Knight'}],
            101: [{'player_name_last_seen': 'Moon Knight Junior'}],
        }
        target = replace(target, account_links=links)
        exact = await find_discord_members(target, {'query': 'Moon Knight'})
        self.assertEqual([row['member_id'] for row in exact['members']], [42])
        qualified = await find_discord_members(target, {'query': 'Ahmad amaj'})
        self.assertEqual([row['member_id'] for row in qualified['members']], [101])
        self.assertNotIn(synthetic_test.id, links.get_links_for_members.call_args.args[0])

    async def test_every_name_part_matches_across_discord_names(self):
        for query in ("ahmad (amaj)", "Ahmad amaj", "AMAj AHMAD"):
            with self.subTest(query=query):
                target = context()
                result = await find_discord_members(target, {"query": query})
                self.assertEqual([row["member_id"] for row in result["members"]], [101])
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(target.state.resolved_members, {101: query})

    async def test_server_nickname_and_known_alias_are_searchable(self):
        target = context()
        target.guild.members[1].nick = "Captain"
        target.guild.members[1].aliases = ("Old Alias",)
        for query in ("Ahmad Captain", "Ahmad Old Alias"):
            result = await find_discord_members(target, {"query": query})
            self.assertEqual([row["member_id"] for row in result["members"]], [101])

    async def test_linked_account_name_can_disambiguate_but_cannot_drop_first_name(self):
        from dataclasses import replace
        links = Mock()
        links.get_links_for_members.return_value = {
            42: [], 101: [{"player_name_last_seen": "Moon Knight"}],
        }
        target = replace(context(), account_links=links)
        result = await find_discord_members(target, {"query": "Ahmad (Moon Knight)"})
        self.assertEqual([row["member_id"] for row in result["members"]], [101])
        missing = await find_discord_members(target, {"query": "Dane Moon Knight"})
        self.assertEqual(missing["members"], [])
        self.assertEqual(missing["status"], "needs_input")
        links.get_links_for_members.assert_called_with([42, 101])

    async def test_unmatched_qualifier_never_falls_back_to_shared_first_name(self):
        target = context()
        result = await find_discord_members(target, {"query": "Ahmad (unknown)"})
        self.assertEqual(result["members"], [])
        self.assertIn("Ahmad (unknown)", result["missing"][0])
        self.assertEqual(target.state.resolved_members, {})

    async def test_ambiguous_results_grant_no_target_provenance(self):
        target = context()
        target.guild.members.extend(member(i, f"ahmad{i}") for i in range(102, 111))
        result = await find_discord_members(target, {"query": "Ahmad"})
        self.assertEqual(len(result["members"]), 8)
        self.assertEqual(result["matched_count"], 11)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(target.state.resolved_members, {})

    async def test_mentions_and_ids_are_exact(self):
        for query in ("101", "<@101>", "<@!101>"):
            result = await find_discord_members(context(), {"query": query})
            self.assertEqual([row["member_id"] for row in result["members"]], [101])

    async def test_name_qualifier_must_also_match_an_explicit_mention(self):
        for query, expected in (("<@101> (amaj)", [101]), ("<@42> (amaj)", [])):
            result = await find_discord_members(context(), {"query": query})
            self.assertEqual([row["member_id"] for row in result["members"]], expected)

    async def test_blank_social_answer_gets_one_generated_text_line(self):
        session = _AgentSession([AgentStep(" \n\t ", (), AgentUsage()),
                                 AgentStep("Synthetic greeting.", (), AgentUsage())])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="hello", local_context="", context=context(),
        )
        self.assertEqual(answer, "Synthetic greeting.")
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(session.calls[-1][1])

    async def test_tool_only_plan_claiming_delivery_without_message_ids_gets_fallback(self):
        target = context()
        target.state.reply_delivery.posts.append({"status": "complete", "message_ids": []})
        service = AgentService(None)
        with patch.object(service, "_answer", AsyncMock(return_value="")):
            answer = await service.answer(question="hello", local_context="", context=target)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
