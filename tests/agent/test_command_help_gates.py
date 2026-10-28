"""Staff help obeys role unions, destination audiences, and revocation."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from elbow_helper.configuration import roles
from elbow_helper.agent.access import (
    ACCESS_ROLE_SETS, AgentAccessLost, require_evidence_access,
)
from elbow_helper.agent.commands.help_tool import read_bot_command_help
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.features.help.catalog import HELP_ENTRIES, HelpEntry
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from agent import test_evidence_scope as fixtures


class CommandHelpGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_path_takes_precedence_over_redundant_search(self):
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                     fixtures.Role(roles.CO_LEADER_ROLE_ID)]
        result = await read_bot_command_help(self.context, {'query': 'health', 'command_path': '/health'})
        self.assertIn('/health clan', {r['path'] for r in result['commands']})
        detail = await read_bot_command_help(self.context, {'query': 'unrelated', 'command_path': '/health clan'})
        self.assertEqual(detail['path'], '/health clan')

    async def test_requester_overview_matches_help_roles_and_reaches_both_contexts(self):
        import json
        from elbow_helper.agent.conversation.context import compile_context, requester_command_overview
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                     fixtures.Role(roles.CO_LEADER_ROLE_ID)]
        overview = requester_command_overview(self.context)
        expected = {entry.path for entry in HELP_ENTRIES if entry.visible_to is None
                    or roles.CO_LEADER_ROLE_ID in entry.visible_to}
        self.assertEqual({r['path'] for r in overview['commands']}, expected)
        compiled = compile_context(question='Discuss a command feature', local_context='',
            context=self.context, tools=(), conversation_history='')
        self.assertIn('requester_command_overview', compiled.prompt)
        context = json.loads(compiled.answer_context)
        self.assertEqual({r['path'] for r in context['requester_command_overview']['commands']}, expected)
        self.assertNotIn('/health clan', {r['path'] for r in context['bot_command_overview']['commands']})
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_exact_subcommand_returns_options_and_shared_scope_reports_its_gap(self):
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                     fixtures.Role(roles.CO_LEADER_ROLE_ID)]
        self.discovered['/health clan'] = DiscoveredCommand('/health clan', 'Health report',
            (ParameterInfo('period', 'Report window', False, 'string'),))
        result = await read_bot_command_help(self.context, {'command_path': '/health clan'})
        self.assertEqual(result['path'], '/health clan')
        self.assertEqual(result['options'][0]['name'], 'period')
        self.context = replace(self.context, private_pass=False, state=AgentTurnState())
        self.context.source_message.channel = self.context.guild.channels[1]
        refused = await read_bot_command_help(self.context, {'command_path': '/health clan'})
        self.assertEqual(refused['cause'], 'scope_refused')
        self.assertTrue(refused['available_privately'])
        self.assertNotIn('options', refused)
        self.assertFalse(self.context.state.required_access)

    async def test_default_family_catalogue_can_verify_trial_commands_through_existing_gates(self):
        from elbow_helper.agent.engine.service import AgentService
        from agent.engine.test_agent_plan_flow import _Model, _Session
        flow, _ = AgentService(_Model(_Session([], [])))._start_flow(question='Verify trial controls',
            local_context='', context=self.context, conversation_history='')
        help_tool = flow.registry['read_bot_command_help']
        for command in ('/accept', '/checkup', '/finalize'):
            result = await help_tool.handler(self.context, {'query': command})
            self.assertEqual(result['path'], command)
            self.assertTrue(result['details'])
            self.assertEqual(result['source'], '/help')
        self.assertTrue(self.context.state.required_access)
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        discovered = {entry.path: DiscoveredCommand(entry.path, entry.summary, ())
                      for entry in HELP_ENTRIES}
        self.discovery = patch(
            "elbow_helper.agent.commands.help_tool.discover_commands",
            return_value=discovered,
        )
        self.discovered = self.discovery.start().return_value
        self.addCleanup(self.discovery.stop)

    def test_every_staff_help_audience_has_retained_access_group(self):
        for entry in HELP_ENTRIES:
            if entry.visible_to is not None:
                self.assertIn(entry.visible_to, ACCESS_ROLE_SETS.values(), entry.path)

    async def test_every_named_operator_gets_own_help(self):
        for group, command in ((roles.LEAD_PLUS, "/roster list"),
                               (roles.RECRUITERS, "/recstats"),
                               (roles.CWL_HELPERS, "/cwl brief")):
            for role_id in group:
                with self.subTest(role_id=role_id, command=command):
                    self.context = replace(self.context, state=AgentTurnState())
                    self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                                 fixtures.Role(role_id)]
                    result = await read_bot_command_help(self.context, {"query": command})
                    self.assertEqual(result["path"], command)
                    self.assertTrue(self.context.state.required_access)

    async def test_examiner_audience_uses_same_role_and_destination_rules(self):
        entry = HelpEntry("/examiner", "Examiner help", "Details", "Examination",
                          frozenset(roles.EXAMINERS | roles.LEAD_PLUS))
        with patch("elbow_helper.agent.commands.help_tool.HELP_ENTRIES", (entry,)):
            self.discovered[entry.path] = DiscoveredCommand(entry.path, entry.summary, ())
            for role_id in roles.EXAMINERS:
                self.context = replace(self.context, state=AgentTurnState())
                self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                             fixtures.Role(role_id)]
                self.assertEqual((await read_bot_command_help(
                    self.context, {"query": entry.path}))["path"], entry.path)

    async def test_shared_scope_excludes_staff_help_but_private_scope_allows_it(self):
        self.context = replace(self.context, private_pass=False)
        self.context.source_message.channel = self.context.guild.channels[1]
        result = await read_bot_command_help(self.context, {"query": "recstats"})
        self.assertEqual(result["matching_count"], 0)
        self.assertFalse(self.context.state.required_access)
        self.assertTrue(self.context.state.scope_audit.private_candidates)
        self.context = replace(self.context, private_pass=True)
        result = await read_bot_command_help(self.context, {"query": "recstats"})
        self.assertEqual(result["path"], "/recstats")
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_list_denies_unauthorized_help_and_retains_only_returned_requirements(self):
        result = await read_bot_command_help(self.context, {"query": "coinlog"})
        self.assertEqual(result["matching_count"], 0)
        public = await read_bot_command_help(self.context, {"query": "ping"})
        self.assertEqual(public["path"], "/ping")
        self.assertFalse(self.context.state.required_access)
        result = await read_bot_command_help(self.context, {"query": "recstats", "limit": 1})
        self.assertEqual(result["path"], "/recstats")
        self.assertEqual(self.context.state.required_access, {"recruiter_or_core"})

    async def test_shared_recruiter_help_list_is_allowed_and_retained(self):
        self.context = replace(self.context, private_pass=False)
        result = await read_bot_command_help(self.context, {"query": "rec", "limit": 1})
        self.assertEqual(len(result["commands"]), 1)
        self.assertTrue(self.context.state.required_access)
        await require_evidence_access(self.context)

    async def test_recruitment_room_with_core_observer_exposes_accept_and_rechecks_operator(self):
        self.context = replace(self.context, private_pass=False)
        self.context.guild.get_member(11).roles = [fixtures.Role(next(iter(roles.CORE)))]
        result = await read_bot_command_help(self.context, {'query': 'trial', 'limit': 20})
        self.assertIn('/accept', {entry['path'] for entry in result['commands']})
        self.assertIn('recruitment', self.context.state.required_access)
        self.assertIn('lead_or_recruiter', self.context.state.help_requester_access)
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID),
                                     fixtures.Role(next(iter(roles.CORE)))]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_public_help_does_not_mark_unrelated_staff_sources_relevant(self):
        self.context = replace(self.context, private_pass=False)
        self.context.source_message.channel = self.context.guild.channels[1]
        result = await read_bot_command_help(self.context, {"query": "ping"})
        self.assertEqual(result["path"], "/ping")
        self.assertFalse(self.context.state.scope_audit.private_candidates)
