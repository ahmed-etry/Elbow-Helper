"""Synthetic assessment recommendations share checked plans and Confirm controls."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.agent.capabilities.account_links.commands import account_link_adapters
from elbow_helper.agent.capabilities.hibernation.commands import hibernation_adapters
from elbow_helper.agent.capabilities.recruitment.commands import recruitment_adapters
from elbow_helper.agent.capabilities.rosters.signups import roster_account_management_tools
from elbow_helper.agent.commands.bridge import build_command_tools
from elbow_helper.agent.discord_actions.roles import discord_role_tools
from elbow_helper.agent.discord_actions.timeouts import timeout_tools
from elbow_helper.agent.engine.assessment import decision_answer
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.recommended_actions import action_catalogue
from elbow_helper.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.agent.wording import ACTION_PREVIEW_OWNER
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine import test_request_cost as cost
from agent.engine.test_agent_plan_flow import _model_step


def decision(items=(), **overrides):
    return json.dumps({'intent': 'Choose the next step for the selected people',
        'intent_source': 'request', 'discussion_message_ids': [],
        'settled_choices': [], 'open_choices': [], 'facts': 'Synthetic observations.',
        'question': '', 'recommendation': 'Synthetic recommendation.',
        'recommended_actions': list(items), **overrides})


def action(member=51, name='synthetic_change', **arguments):
    return {'capability': name, 'target': {'member_id': member},
            'arguments': {'member_id': member, **arguments}}


class RecommendedActionTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, content, *, registry=None, **context_fields):
        self.run_change = AsyncMock()
        self.recheck = AsyncMock(return_value=True)

        async def prepare(context, arguments):
            context.state.proposed_changes.append(PreparedAction(
                'synthetic_change', arguments,
                ChangePreview(('Synthetic checked preview.',), self.recheck), self.run_change))
            return {'status': 'confirmation_required'}

        self.prepare = AsyncMock(side_effect=prepare)
        tool = RegisteredAgentTool(AgentToolDefinition('synthetic_change', 'Synthetic change.', {
            'type': 'object', 'properties': {'member_id': {'type': 'integer', 'minimum': 1}},
            'required': ['member_id'], 'additionalProperties': False}),
            self.prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(('member_id', 'discord_member'),)))
        selected = registry or {'synthetic_change': tool}
        flow, _, _, _ = cost.RequestCostTests().flow([], registry=selected.copy())
        # The fixture has no Discord command tree; install its synthetic changes explicitly.
        flow.registry.update(selected)
        flow.recommendation_registry = flow.registry.copy()
        context = replace(flow.context, **context_fields)
        context.state.resolved_members.update({51: 'Synthetic A', 52: 'Synthetic B', 53: 'Synthetic C'})
        flow.context = flow.runner.context = context
        flow.assessment_requested = True
        flow.rounder.final_attempted = True
        flow._run = AsyncMock(return_value=content)
        return flow

    async def test_finished_recommendation_is_a_preview_without_execution(self):
        flow = self.flow(decision([action()]))
        answer = await flow.run()
        self.assertEqual(answer, 'Synthetic observations.\n\nSynthetic recommendation.')
        self.prepare.assert_awaited_once()
        proposals = flow.context.state.proposed_changes
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].capability_name, 'synthetic_change')
        self.assertEqual(proposals[0].checked_arguments, {'member_id': 51})
        self.assertEqual(flow.context.state.preview_reply, preview_text(proposals))
        self.assertFalse(flow.context.state.preview_first)
        self.assertIn(proposals[0].step_id, flow.runner.completed_steps)
        self.run_change.assert_not_awaited()
        self.recheck.assert_not_awaited()

        runner = SimpleNamespace(submit=AsyncMock())
        view = ConfirmationView(flow.context.member.id, tuple(proposals), flow.context, runner=runner)
        self.addCleanup(view.stop)
        other = SimpleNamespace(user=SimpleNamespace(id=52), response=SimpleNamespace(send_message=AsyncMock()))
        await view.confirm(other)
        other.response.send_message.assert_awaited_once_with(ACTION_PREVIEW_OWNER, ephemeral=True)
        runner.submit.assert_not_awaited()
        requester = SimpleNamespace(user=flow.context.member,
                                    response=SimpleNamespace(defer=AsyncMock()))
        await view.confirm(requester)
        runner.submit.assert_awaited_once_with(flow.context, tuple(proposals),
            confirmer_id=flow.context.member.id, progress_message=None)
        self.run_change.assert_not_awaited()

    async def test_unresolved_or_substituted_members_are_not_offered(self):
        for item in (action(99), {**action(), 'target': {'member_id': 52}},
                     action(member_id={'step': 'old', 'path': ['member_id']})):
            with self.subTest(item=item):
                flow = self.flow(decision([item]))
                flow.context.state.grounding_evidence['roster'] = {
                    'rows': [{'member_id': 99, 'player_tag': '#P0Y'}]}
                await flow.run()
                self.prepare.assert_not_awaited()
                self.assertFalse(flow.context.state.proposed_changes)
                self.assertIsNone(flow.context.state.preview_reply)

    async def test_mention_and_explicit_self_provenance_remain_valid(self):
        for provenance in ('mention', 'self'):
            with self.subTest(provenance=provenance):
                target = 99 if provenance == 'mention' else 41
                flow = self.flow(decision([action(target)]))
                flow.context.source_message.raw_mentions = [target] if provenance == 'mention' else []
                flow.context.state.member_self_target = provenance == 'self'
                await flow.run()
                self.prepare.assert_awaited_once()

    async def test_empty_actions_and_unsettled_answers_never_prepare_changes(self):
        for fields in ({'recommended_actions': []}, {'recommendation': ''}, {'recommendation': ' \t'},
                       {'intent_source': 'unresolved', 'question': 'Which next step?'},
                       {'open_choices': ['Approach'], 'question': 'Which approach?'}):
            with self.subTest(fields=fields):
                flow = self.flow(decision([action()], **fields))
                # Action-like request wording cannot override the structural gates.
                flow.context.state.request_text = 'Hibernate everyone and give them roles'
                await flow.run()
                self.prepare.assert_not_awaited()
                self.assertFalse(flow.context.state.proposed_changes)

    async def test_omitted_action_declarations_are_not_offers(self):
        content = json.loads(decision())
        del content['recommended_actions']
        flow = self.flow(json.dumps(content))
        await flow.run()
        self.prepare.assert_not_awaited()

    async def test_factual_answer_with_empty_actions_offers_nothing(self):
        flow = self.flow(decision(intent='Summarize synthetic statistics.',
                                  facts='Synthetic count: 7.', recommendation=''))
        self.assertEqual(await flow.run(), 'Synthetic count: 7.')
        self.prepare.assert_not_awaited()
        self.assertIsNone(flow.context.state.preview_reply)

    async def test_action_list_is_not_gated_by_intent_or_request_text(self):
        flow = self.flow(decision([action()], intent='Summarize synthetic statistics.'))
        flow.context.state.request_text = 'Synthetic count: 7.'
        await flow.run()
        self.prepare.assert_awaited_once()
        self.run_change.assert_not_awaited()

    async def test_validation_skips_invalid_unknown_read_and_untyped_capabilities(self):
        for case in ('invalid_arguments', 'unknown', 'read', 'untyped'):
            with self.subTest(case=case):
                item = action()
                if case == 'invalid_arguments':
                    item['arguments']['extra'] = True
                elif case == 'unknown':
                    item['capability'] = 'not_registered'
                flow = self.flow(decision([item]))
                if case in ('read', 'untyped'):
                    original = flow.registry['synthetic_change']
                    flow.registry['synthetic_change'] = replace(original,
                        **({'action_class': ActionClass.READ} if case == 'read' else {'contract': None}))
                    flow.recommendation_registry = flow.registry.copy()
                await flow.run()
                self.prepare.assert_not_awaited()

    async def test_account_targets_must_match_authorized_evidence(self):
        handler = AsyncMock()
        tool = RegisteredAgentTool(AgentToolDefinition('account_change', 'Synthetic account change.', {
            'type': 'object', 'properties': {'player_tag': {'type': 'string'}},
            'required': ['player_tag'], 'additionalProperties': False}), handler,
            AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(('player_tag', 'clash_account'),)))
        for tag, declared, expected in (('#P0Y', '#P0Y', 1), ('#P0Q', '#P0Q', 0),
                                        ('#P0Q', '#P0Y', 0)):
            with self.subTest(tag=tag, declared=declared):
                handler.reset_mock()
                item = {'capability': 'account_change', 'target': {'player_tag': declared},
                        'arguments': {'player_tag': tag}}
                flow = self.flow(decision([item]), registry={'account_change': tool})
                flow.context.state.grounding_evidence['accounts'] = {
                    'rows': [{'player_tag': '#P0Y', 'player_name': 'Synthetic account'}]}

                async def prepare(context, arguments):
                    context.state.proposed_changes.append(PreparedAction('account_change', arguments,
                        ChangePreview(('Synthetic account preview.',), self.recheck), self.run_change))
                    return {'status': 'confirmation_required'}

                handler.side_effect = prepare
                await flow.run()
                self.assertEqual(handler.await_count, expected)
                self.assertEqual(len(flow.context.state.proposed_changes), expected)

    async def test_opaque_account_add_and_accept_adapters_cannot_offer_hidden_accounts(self):
        adapters = (account_link_adapters()[0],
                    next(adapter for adapter in recruitment_adapters() if adapter.path == '/accept'))
        preparations = {adapter.path: AsyncMock(return_value=ChangePreview(
            ('Synthetic account changes.',), AsyncMock(return_value=True), count=2))
            for adapter in adapters}
        adapters = tuple(replace(adapter, prepare=preparations[adapter.path], run=AsyncMock())
                         for adapter in adapters)
        commands = {
            '/account add': DiscoveredCommand('/account add', 'Synthetic add.', (
                ParameterInfo('member', 'Member.', True, 'user'),
                ParameterInfo('tags', 'Accounts.', True, 'string'))),
            '/accept': DiscoveredCommand('/accept', 'Synthetic accept.', (
                ParameterInfo('applicant', 'Applicant.', True, 'user'),
                ParameterInfo('accounts', 'Accounts.', True, 'string')))}
        with patch('elbow_helper.agent.commands.registry.discover_commands', return_value=commands):
            tools, capabilities = build_command_tools(object(), adapters)
        for name, member_field, account_field in (
                ('account_add', 'member', 'tags'), ('accept_applicant', 'applicant', 'accounts')):
            for established in ((), ('#P0Y', '#P0Q')):
                with self.subTest(capability=name, established=established):
                    item = {'capability': name, 'target': {'member_id': 51},
                            'arguments': {member_field: 51, account_field: '#P0Y #P0Q'}}
                    flow = self.flow(decision([item]), registry=tools)
                    flow.context.member.roles.extend(SimpleNamespace(id=identifier)
                        for identifier in capabilities[name].visible_to or ())
                    flow.answer_accounts = {tag: {'name': tag} for tag in established}
                    # These are real bridge contracts: the owner is typed, the
                    # account-bearing string is absent from the contract.
                    self.assertIn((member_field, 'discord_member'), tools[name].contract.entity_fields)
                    catalogue = json.loads(action_catalogue(flow.recommendation_registry))
                    self.assertEqual(catalogue['recommended_action_capabilities'], [])
                    await flow.run()
                    self.assertFalse(flow.context.state.proposed_changes)
                    self.assertIsNone(flow.context.state.preview_reply)
        for adapter in adapters:
            preparations[adapter.path].assert_not_awaited()
            adapter.run.assert_not_awaited()

    async def test_incomplete_open_or_indirect_contracts_are_excluded(self):
        for case in ('uncovered', 'structured_filter', 'open_schema', 'indirect_selector', 'variant'):
            with self.subTest(case=case):
                properties = {'member_id': {'type': 'integer', 'minimum': 1}}
                arguments = {'member_id': 51}
                entities = [('member_id', 'discord_member')]
                filters = ()
                if case in ('uncovered', 'structured_filter'):
                    properties['selection'] = {'type': 'object', 'additionalProperties': True}
                    arguments['selection'] = {'accounts': ['#P0Y', '#P0Q']}
                    filters = ('selection',) if case == 'structured_filter' else ()
                elif case == 'indirect_selector':
                    properties['roster_id'] = {'type': 'integer'}
                    arguments['roster_id'] = 1
                    entities.append(('roster_id', 'roster'))
                schema = {'type': 'object', 'properties': properties,
                          'required': ['member_id'], 'additionalProperties': case == 'open_schema'}
                if case == 'variant':
                    schema['anyOf'] = [{'properties': {'tags': {'type': 'string'}}}]
                handler = AsyncMock()
                tool = RegisteredAgentTool(AgentToolDefinition('partial_change', 'Synthetic change.', schema),
                    handler, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
                    contract=CapabilityContract(entity_fields=tuple(entities), filter_fields=filters))
                item = {'capability': 'partial_change', 'target': {'member_id': 51}, 'arguments': arguments}
                flow = self.flow(decision([item]), registry={'partial_change': tool})
                await flow.run()
                handler.assert_not_awaited()
                self.assertFalse(flow.context.state.proposed_changes)

    async def test_every_typed_target_and_the_whole_target_count_are_checked(self):
        for kind, values, declared, evidence in (
                ('discord_member', [51, 99], {'member_id': 51}, {}),
                ('discord_member', [51, 52], {'member_id': 51}, {}),
                ('clash_account', ['#P0Y', '#P0Q'], {'player_tag': '#P0Y'}, {'#P0Y': {}}),
                ('clash_account', ['#P0Y', '#P0Q'], {'player_tag': '#P0Y'}, {'#P0Y': {}, '#P0Q': {}})):
            with self.subTest(kind=kind, values=values, evidence=evidence):
                handler = AsyncMock()
                tool = RegisteredAgentTool(AgentToolDefinition('typed_change', 'Synthetic change.', {
                    'type': 'object', 'properties': {'targets': {'type': 'array', 'items': {
                        'type': 'integer' if kind == 'discord_member' else 'string'}}},
                    'required': ['targets'], 'additionalProperties': False}), handler,
                    AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
                    contract=CapabilityContract(entity_fields=(('targets', kind + '_set'),)))
                item = {'capability': 'typed_change', 'target': declared, 'arguments': {'targets': values}}
                flow = self.flow(decision([item]), registry={'typed_change': tool})
                flow.answer_accounts = evidence
                await flow.run()
                handler.assert_not_awaited()
                self.assertFalse(flow.context.state.proposed_changes)

    async def test_missing_or_mixed_member_and_account_targets_are_not_offered(self):
        handler = AsyncMock()
        tool = RegisteredAgentTool(AgentToolDefinition('mixed_change', 'Synthetic change.', {
            'type': 'object', 'properties': {'member_id': {'type': 'integer'}, 'player_tag': {'type': 'string'}},
            'required': ['player_tag'], 'additionalProperties': False}), handler,
            AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(('member_id', 'discord_member'),
                                                      ('player_tag', 'clash_account'))))
        for arguments in ({'player_tag': '#P0Y'}, {'player_tag': '#P0Y', 'member_id': 51}):
            with self.subTest(arguments=arguments):
                item = {'capability': 'mixed_change', 'target': {'player_tag': '#P0Y'}, 'arguments': arguments}
                flow = self.flow(decision([item]), registry={'mixed_change': tool})
                flow.answer_accounts = {'#P0Y': {}}
                await flow.run()
                handler.assert_not_awaited()
                self.assertFalse(flow.context.state.proposed_changes)

    async def test_optional_target_fields_cannot_hide_default_members(self):
        handler = AsyncMock()
        tool = RegisteredAgentTool(AgentToolDefinition('default_member_change', 'Synthetic change.', {
            'type': 'object', 'properties': {'member_id': {'type': 'integer'},
                                           'other_member': {'type': 'integer'}},
            'required': ['member_id'], 'additionalProperties': False}), handler,
            AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(('member_id', 'discord_member'),
                                                      ('other_member', 'discord_member'))))
        flow = self.flow(decision([action(name='default_member_change')]),
                         registry={'default_member_change': tool})
        await flow.run()
        handler.assert_not_awaited()
        self.assertFalse(flow.context.state.proposed_changes)

    def test_roster_adapters_with_implicit_owners_are_not_in_the_catalogue(self):
        tools = {tool.definition.name: tool for tool in roster_account_management_tools()}
        self.assertEqual(json.loads(action_catalogue(tools))['recommended_action_capabilities'], [])

    async def test_duplicates_and_large_preparations_stay_bounded(self):
        flow = self.flow(decision([action(), action(), action(52)]))
        await flow.run()
        self.assertEqual(len(flow.context.state.proposed_changes), 2)
        self.assertEqual(self.prepare.await_count, 2)
        flow = self.flow(decision([action(), action(52), action(53)]))
        await flow.run()
        self.assertEqual(len(flow.context.state.proposed_changes), 3)
        original = self.prepare.side_effect

        async def too_many(context, arguments):
            for _ in range(4):
                await original(context, arguments)
            return {'status': 'confirmation_required'}

        flow = self.flow(decision([action()]))
        self.prepare.side_effect = too_many
        await flow.run()
        self.assertFalse(flow.context.state.proposed_changes)
        self.run_change.assert_not_awaited()

    async def test_irreversible_changes_keep_their_own_bundle_rules(self):
        flow = self.flow(decision([action(), action(52, name='irreversible')]))
        irreversible = replace(flow.registry['synthetic_change'], action_class=ActionClass.IRREVERSIBLE)
        flow.registry['irreversible'] = replace(irreversible,
            definition=replace(irreversible.definition, name='irreversible'))
        flow.recommendation_registry = flow.registry.copy()
        await flow.run()
        self.assertEqual(len(flow.context.state.proposed_changes), 1)
        self.assertEqual(self.prepare.await_count, 1)

    async def test_repaired_assessment_uses_only_the_validated_actions(self):
        flow = self.flow('invalid')
        flow._repair_decision_format = AsyncMock(return_value=SimpleNamespace(content=decision([action()])))
        await flow.run()
        self.prepare.assert_awaited_once()

    async def test_tool_disabled_final_composition_can_prepare_the_recommendation(self):
        flow = self.flow(decision())
        del flow._run
        flow.assessment_requested = False
        flow.rounder.final_attempted = False
        flow.final_session_factory = None
        read = RegisteredAgentTool(AgentToolDefinition('read_value', 'Synthetic read.', {
            'type': 'object', 'properties': {}, 'additionalProperties': False}),
            AsyncMock(return_value={'value': 7}))
        flow.registry['read_value'] = read
        plan = {'goal': 'Assess the selected people', 'effort': 'high', 'assessment': True,
                'finish_after_reads': True, 'output': 'text', 'steps': [
                    {'id': 'read', 'capability': 'read_value', 'arguments': {}, 'depends_on': []}]}
        flow.session.steps = [_model_step(plan), cost.response(decision([action()]))]
        answer = await flow.run()
        self.assertEqual(answer, 'Synthetic observations.\n\nSynthetic recommendation.')
        self.assertEqual(len(flow.session.calls), 2)
        self.assertFalse(flow.session.calls[-1][1])
        self.assertIn('recommended_action_capabilities', flow.session.instructions[-1])
        self.prepare.assert_awaited_once()
        self.run_change.assert_not_awaited()

    async def test_hibernation_specialist_uses_its_normal_command_preview(self):
        command = DiscoveredCommand('/hibernate', 'Synthetic hibernation.', (
            ParameterInfo('user', 'Member to hibernate.', True, 'user'),))
        with patch('elbow_helper.agent.commands.registry.discover_commands',
                   return_value={'/hibernate': command}):
            tools, _ = build_command_tools(object(), hibernation_adapters()[:1])
        item = {'capability': 'hibernate_member', 'target': {'member_id': 51},
                'arguments': {'user': 51}}
        flow = self.flow(decision([item]))
        flow.recommendation_registry.update(tools)
        member = SimpleNamespace(id=51, mention='<@51>', roles=[])
        plan = {'issue': None, 'stored_role_ids': [], 'snapshot_role_ids': [], 'unix_ts': 100,
                'to_remove': (), 'to_add': (), 'missing_role_ids': (), 'log_channel_id': 9,
                'fallback_channel_id': 10}
        workflow = SimpleNamespace(prepare_hibernation=lambda *_: plan,
            hibernation_notice_preview=lambda _: 'Synthetic hibernation notice.',
            hibernate_member=AsyncMock(), hibernation_member_state=lambda _: None)
        flow.context = replace(flow.context, bot=SimpleNamespace(get_cog=lambda _: workflow))
        flow.runner.context = flow.context
        requester = flow.context.member
        requester.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        flow.context.guild.get_member = lambda identifier: requester if identifier == requester.id else member
        flow.context.guild.fetch_member = AsyncMock(return_value=member)
        self.assertNotIn('hibernate_member', flow.registry)
        await flow.run()
        proposal = flow.context.state.proposed_changes[0]
        self.assertEqual(proposal.path, '/hibernate')
        self.assertIn('Synthetic hibernation notice.', proposal.preview.details)
        self.assertTrue(await proposal.preview.recheck())
        workflow.hibernate_member.assert_not_awaited()

    async def test_changed_answer_does_not_offer_stale_recommendations(self):
        flow = self.flow(decision([action()]))
        flow._checked_member_reply = AsyncMock(return_value='A corrected assessment.')
        self.assertEqual(await flow.run(), 'A corrected assessment.')
        self.prepare.assert_not_awaited()

    async def test_existing_permission_and_hierarchy_refusals_are_preserved(self):
        tool = timeout_tools()[0]
        item = action(name=tool.definition.name, operation='remove')
        for failure, expected in (
                ('requester', 'You need Moderate Members in this server.'),
                ('bot', 'The bot needs Moderate Members in this server.'),
                ('hierarchy', "That member must be below your highest role and the bot's.")):
            with self.subTest(failure=failure):
                flow = self.flow(decision([item]), registry={tool.definition.name: tool})
                actor = flow.context.member
                actor.guild_permissions = discord.Permissions(moderate_members=failure != 'requester')
                actor.top_role = SimpleNamespace(position=10)
                bot = flow.context.guild.me
                bot.guild_permissions = discord.Permissions(moderate_members=failure != 'bot')
                bot.top_role = SimpleNamespace(position=20)
                target = SimpleNamespace(id=51, top_role=SimpleNamespace(position=30), timeout=AsyncMock())
                flow.context.guild.owner_id = 9999
                flow.context.guild.get_member = lambda identifier: {41: actor, 51: target, 999: bot}.get(identifier)
                flow.context.guild.fetch_member = AsyncMock(return_value=target)
                answer = await flow.run()
                self.assertEqual(answer, 'Synthetic observations.\n\nSynthetic recommendation.\n\n' + expected)
                self.assertFalse(flow.context.state.proposed_changes)
                target.timeout.assert_not_awaited()

    async def test_role_recommendation_uses_the_existing_role_delta_and_recheck(self):
        tool = next(tool for tool in discord_role_tools() if tool.definition.name == 'add_discord_roles')
        item = {'capability': tool.definition.name, 'target': {'member_id': 51},
                'arguments': {'role_id': 77, 'member_ids': [51]}}
        flow = self.flow(decision([item]), registry={tool.definition.name: tool})
        actor = flow.context.member
        actor.guild_permissions = discord.Permissions(manage_roles=True)
        actor.top_role = SimpleNamespace(position=10)
        role = SimpleNamespace(id=77, name='Synthetic role', mention='<@&77>', position=1,
            managed=False, permissions=discord.Permissions.none(), is_default=lambda: False)
        member = SimpleNamespace(id=51, mention='<@51>', display_name='Synthetic A', roles=[], add_roles=AsyncMock())
        flow.context.guild.owner_id = 9999
        flow.context.guild.get_member = lambda identifier: actor if identifier == 41 else member
        flow.context.guild.get_role = lambda identifier: role if identifier == 77 else None
        flow.context.guild.fetch_member = AsyncMock(return_value=member)
        with patch('elbow_helper.agent.discord_actions.roles.managed_role_commands',
                   AsyncMock(return_value={})), patch(
                   'elbow_helper.agent.discord_actions.roles.check_member_role_ownership'):
            await flow.run()
            proposal = flow.context.state.proposed_changes[0]
            self.assertIn('Add <@&77> to <@51>.', proposal.preview.lines)
            self.assertTrue(await proposal.preview.recheck())
            actor.guild_permissions.manage_roles = False
            self.assertFalse(await proposal.preview.recheck())
        member.add_roles.assert_not_awaited()

    def test_catalogue_includes_only_registered_targeted_changes(self):
        flow = self.flow(decision())
        catalogue = json.loads(action_catalogue(flow.registry))['recommended_action_capabilities']
        self.assertEqual([item['capability'] for item in catalogue], ['synthetic_change'])
        self.assertIn('member_id', catalogue[0]['arguments']['properties'])
        self.assertIn('recommended_action_capabilities', flow._decision_instruction())
        self.assertNotIn('intent_kind', flow._decision_instruction())

    def test_more_than_three_declarations_fail_the_decision_schema(self):
        with self.assertRaises(ValueError):
            decision_answer(decision([action()] * 4), set())
