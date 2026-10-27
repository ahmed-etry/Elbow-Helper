"""Synthetic named-account resolution and dated activity through retained reports."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.features.agent.engine.flow import AnswerFlow
from elbow_helper.features.agent.engine.tool_call import execute_tool
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.families import account_names, identity_reads
from elbow_helper.features.agent.families import account_activity
from elbow_helper.features.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.features.agent.families.model_view import family_model_view
from elbow_helper.features.agent.families.projections import cohort_selection
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from features.agent.engine.test_agent_plan_flow import _context


def rosters(accounts=()):
    return {'items': [{'tag': clan.tag, 'status': 'ok', 'data': {
        'memberList': [{'tag': tag, 'name': name} for tag, name in accounts]
                      if code == 'BEH' else []}}
        for code, clan in CLANS.items()]}


def context_with_names(stored=(), linked=()):
    links = {tag: {'player_tag': tag, 'player_name_last_seen': name, 'discord_user_id': 42}
             for tag, name in linked}
    context = replace(_context(), account_links=SimpleNamespace(
        get_all_links=lambda: links,
        get_links_by_tags=lambda tags: {tag: links.get(tag) for tag in tags},
        get_links_for_members=Mock(side_effect=lambda identifiers: {
            identifier: [row for row in links.values() if row['discord_user_id'] == identifier]
            for identifier in identifiers})),
        cwl_queries=SimpleNamespace(account_names=lambda: [
            {'player_tag': tag, 'player_name': name} for tag, name in stored]))
    context.guild.get_member = lambda identifier: context.member if identifier == context.member.id else None
    return context


def lookup_context(members=(), linked=()):
    context = context_with_names(linked=[(tag, name) for tag, name, _ in linked])
    for tag, _, identifier in linked:
        context.account_links.get_all_links()[tag]['discord_user_id'] = identifier
    context.guild.members = [context.member, *members]
    context.guild.get_member = lambda identifier: next(
        (member for member in context.guild.members if member.id == identifier), None)
    return context


def named_member(identifier, display_name, *, nick=None, bot=False):
    return SimpleNamespace(id=identifier, display_name=display_name, name=f'synthetic_{identifier}',
                           nick=nick, roles=[], joined_at=None, bot=bot)


class UnifiedNameLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_bracketed_alias_and_username_work_through_either_selector(self):
        for selector in ('name', 'account_names'):
            for query in ('Lumen Birch', 'Birch', 'synthetic_42'):
                with self.subTest(selector=selector, query=query):
                    owner = named_member(42, 'Lumen [Birch]', nick='Lumen [Birch]')
                    context = lookup_context([owner], [('#P0', 'Silver Kite', 42)])
                    arguments = {selector: query if selector == 'name' else [query]}
                    with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
                         patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
                        result = await identity_reads.member_accounts(context, arguments)
                    self.assertEqual(result['status'], 'resolved')
                    resolution = result['name_resolutions'][0]
                    self.assertEqual(resolution['candidates'], [])
                    self.assertEqual(resolution['account_status'], 'not_found')
                    self.assertEqual(resolution['member_status'], 'resolved')
                    self.assertEqual(resolution['matching_members'][0]['member_name'], 'Lumen [Birch]')
                    self.assertEqual(resolution['matching_members'][0]['accounts'], [
                        {'player_tag': '#P0', 'player_name': 'Silver Kite'}])
                    self.assertEqual(result['rows'][0]['member_id'], 42)
                    self.assertEqual(context.state.resolved_members, {42: 'Lumen [Birch]'})

    async def test_account_typo_resolves_bot_linked_owner_and_preserves_stats_scope(self):
        for selector in ('name', 'account_names'):
            with self.subTest(selector=selector):
                owner = named_member(42, 'Synthetic Pilot')
                context = lookup_context([owner], [
                    ('#P0', 'Silver Kite', 42), ('#Q0', 'Other Beacon', 42)])
                action = AsyncMock(return_value={'status': 'complete'})
                async def act():
                    return json.loads(await execute_tool(name='synthetic_action', handler=action,
                        context=context, arguments={'member_id': 42, 'text': 'Hello <@42>'},
                        action_class=ActionClass.OUTPUT))
                self.assertIn('error', await act())
                action.assert_not_awaited()
                arguments = {selector: 'Silver Kte' if selector == 'name' else ['Silver Kte']}
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
                     patch.object(identity_reads, 'profiles', AsyncMock(return_value={})) as profiles:
                    result = await identity_reads.member_accounts(context, arguments)
                self.assertEqual(result['status'], 'resolved')
                resolution = result['name_resolutions'][0]
                self.assertEqual(resolution['candidates'][0]['member_id'], 42)
                self.assertEqual(resolution['matching_members'][0]['match_sources'], ['bot_account_link'])
                self.assertEqual({row['player_tag'] for row in resolution['matching_members'][0]['accounts']},
                                 {'#P0', '#Q0'})
                self.assertEqual(profiles.call_args.args[1], ['#P0'])
                self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
                self.assertEqual((await act())['status'], 'complete')
                action.assert_awaited_once()

    async def test_unrelated_member_and_account_survive_writer_and_never_authorize_actions(self):
        for selector in ('name', 'account_names'):
            with self.subTest(selector=selector):
                member = named_member(42, 'Silver [Kite]')
                owner = named_member(43, 'Different Navigator')
                context = lookup_context([member, owner], [
                    ('#P0', 'Silver Kite', 43), ('#Q0', 'Other Beacon', 42)])
                tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
                arguments = {selector: 'Silver Kite' if selector == 'name' else ['Silver Kite']}
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
                     patch.object(identity_reads, 'profiles', AsyncMock()) as profiles:
                    result = await tool.handler(context, arguments)
                    projected = await cohort_writer_evidence(context, {'lookup': result})
                profiles.assert_not_called()
                resolution = answer_evidence(projected, guild=context.guild)['lookup']['name_resolutions'][0]
                self.assertEqual(resolution['status'], 'ambiguous')
                self.assertEqual({row['member_id'] for row in resolution['matching_members']}, {42, 43})
                self.assertEqual(resolution['matching_members'][0]['accounts'], [
                    {'player_tag': '#Q0', 'player_name': 'Other Beacon'}])
                self.assertEqual([(row['player_tag'], row['member_id']) for row in resolution['candidates']],
                                 [('#P0', 43)])
                self.assertEqual(result['rows'], [])
                self.assertEqual(result['status'], 'needs_input')
                self.assertEqual(context.state.resolved_members, {})
                retained = await tool.handler(context, {'report_id': result['report_id']})
                self.assertEqual(family_model_view(retained)['name_resolutions'][0], resolution)
                action = AsyncMock(return_value={'status': 'complete'})
                for identifier in (42, 43):
                    blocked = json.loads(await execute_tool(name='synthetic_action', handler=action,
                        context=context, arguments={'member_id': identifier}, action_class=ActionClass.OUTPUT))
                    self.assertIn('error', blocked)
                action.assert_not_awaited()

    async def test_account_without_a_current_human_bot_link_cannot_authorize_actions(self):
        for identifier, bot in ((None, False), (404, False), (42, True)):
            with self.subTest(identifier=identifier, bot=bot):
                context = lookup_context([named_member(42, 'Synthetic Pilot', bot=bot)],
                    [('#P0', 'Silver Kite', identifier)] if identifier else [])
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                        ('#P0', 'Silver Kite')]))), \
                     patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
                    result = await identity_reads.member_accounts(context, {'name': 'Silver Kte'})
                self.assertEqual(result['status'], 'resolved')
                self.assertEqual(result['name_resolutions'][0]['matching_members'], [])
                self.assertEqual(context.state.resolved_members, {})
                action = AsyncMock()
                blocked = json.loads(await execute_tool(name='synthetic_action', handler=action,
                    context=context, arguments={'member_id': identifier or 42}, action_class=ActionClass.OUTPUT))
                self.assertIn('error', blocked)
                action.assert_not_awaited()

    async def test_exact_linked_account_does_not_hide_another_members_nickname_part(self):
        for selector in ('name', 'account_names'):
            with self.subTest(selector=selector):
                context = lookup_context([
                    named_member(42, 'Lumen [Birch]'), named_member(43, 'Different Navigator')],
                    [('#P0', 'Birch', 43)])
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
                    result = await identity_reads.member_accounts(context, {
                        selector: 'Birch' if selector == 'name' else ['Birch']})
                resolution = result['name_resolutions'][0]
                self.assertEqual(result['status'], 'needs_input')
                self.assertEqual(resolution['status'], 'ambiguous')
                self.assertEqual({row['member_id'] for row in resolution['matching_members']}, {42, 43})
                self.assertEqual(resolution['candidates'][0]['member_id'], 43)
                self.assertEqual(context.state.resolved_members, {})

    async def test_failed_member_chunk_keeps_account_candidate_without_authorizing_owner(self):
        context = lookup_context([named_member(42, 'Synthetic Pilot')], [('#P0', 'Silver Kite', 42)])
        context.guild.chunked = False
        context.guild.chunk = AsyncMock()
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
            result = await identity_reads.member_accounts(context, {'name': 'Silver Kite'})
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['name_resolutions'][0]['status'], 'unknown')
        self.assertEqual(result['name_resolutions'][0]['candidates'][0]['player_tag'], '#P0')
        self.assertEqual(context.state.resolved_members, {})

    async def test_report_reuse_does_not_grant_new_request_provenance(self):
        context = lookup_context([named_member(42, 'Lumen [Birch]')])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
            result = await tool.handler(context, {'name': 'Birch'})
        self.assertIn(42, context.state.resolved_members)
        context.state.resolved_members.clear()
        await tool.handler(context, {'report_id': result['report_id']})
        self.assertEqual(context.state.resolved_members, {})


class AccountNameMatchingTests(unittest.TestCase):
    def test_long_single_word_tolerates_two_missing_letters(self):
        accounts = {'#P0': {'player_tag': '#P0', 'names': ['Veloraurus']}}
        self.assertEqual(account_names.matching_accounts('veloraus', accounts), [accounts['#P0']])

    def test_spacing_punctuation_and_case_do_not_change_exact_identity(self):
        for query, name in (
                ('Glim orb', 'GlimOrb'), ('GlimOrb', 'Glim orb'),
                ('Qq glints', 'QQ GLINTS'), (' glim.orb ', 'GLIM ORB')):
            with self.subTest(query=query, name=name):
                accounts = {'#P0': {'player_tag': '#P0', 'names': [name]}}
                self.assertEqual(account_names.matching_accounts(query, accounts), [accounts['#P0']])

    def test_edit_allowance_scales_per_word_instead_of_per_name(self):
        for query, name, matches in (
                ('Dovle', 'Dovel', True), ('Doel', 'Dovel', True),
                ('Del', 'Dovel', False), ('Velraus', 'Veloraurus', False),
                ('Glim Comte', 'Glum Comet', True), ('glim-comte', 'GLUM.COMET', True)):
            with self.subTest(query=query, name=name):
                accounts = {'#P0': {'player_tag': '#P0', 'names': [name]}}
                self.assertEqual(account_names.matching_accounts(query, accounts),
                                 [accounts['#P0']] if matches else [])

    def test_initials_and_extra_or_missing_words_do_not_match(self):
        for query, name in (
                ('navor talo', 'Navor.T'), ('navortalo', 'Navor.T'),
                ('Glim', 'Glim Orb'), ('Glim Orb', 'Glim'),
                ('Glim A Orb', 'Glim Orb'), ('Glim Orb', 'Glim A Orb'),
                ('Glim Orb Kite', 'Glim Orb'), ('Glim Orb', 'Glim Orb Kite')):
            with self.subTest(query=query, name=name):
                accounts = {'#P0': {'player_tag': '#P0', 'names': [name]}}
                self.assertEqual(account_names.matching_accounts(query, accounts), [])


class NamedAccountStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_spacing_and_spelling_select_tags_without_discord_or_alt_expansion(self):
        context = context_with_names(stored=[('#Y0', 'Nebulous')],
            linked=[('#P0', 'Amber Comet'), ('#Q0', 'Other Beacon')])
        profiles = AsyncMock(return_value={
            '#P0': {'status': 'ok', 'data': {'name': 'Amber Comet'}},
            '#Y0': {'status': 'ok', 'data': {'name': 'Nebulous'}}})
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Amber Comet')]))), patch.object(identity_reads, 'profiles', profiles):
            result = await identity_reads.member_accounts(context,
                {'account_names': [' aMbErCoMet ', 'Nebluous']})
        self.assertEqual([row['status'] for row in result['name_resolutions']], ['resolved', 'resolved'])
        self.assertEqual({row['player_tag'] for row in result['rows']}, {'#P0', '#Y0'})
        self.assertEqual(profiles.call_args.args[1], ['#P0', '#Y0'])
        self.assertTrue(result['selection_complete'])
        self.assertIsNone(next(row for row in result['rows'] if row['player_tag'] == '#Y0')['member_id'])
        self.assertEqual(context.state.resolved_members, {})

    async def test_duplicate_normalized_names_and_plausible_typos_are_explicitly_ambiguous(self):
        for query, names in (
                ('Silver Kite', [('#P0', 'SilverKite'), ('#Q0', 'Silver Kite')]),
                ('Silver Kte', [('#P0', 'Silver Kite'), ('#Q0', 'Silver Kate')])):
            with self.subTest(query=query):
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters(names))):
                    result = await account_names.resolve_account_names(context_with_names(), [query])
                self.assertEqual(result['accounts'], {})
                self.assertFalse(result['selection_complete'])
                self.assertEqual(result['name_resolutions'][0]['status'], 'ambiguous')
                self.assertEqual({row['player_tag'] for row in result['name_resolutions'][0]['candidates']},
                                 {'#P0', '#Q0'})

    async def test_exact_name_wins_and_renamed_aliases_deduplicate_by_tag(self):
        context = context_with_names(stored=[('#P0', 'Copper Finch')],
                                     linked=[('#P0', 'Copper Finch')])
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Golden Finch'), ('#Q0', 'Copper Pinch')]))):
            result = await account_names.resolve_account_names(context, ['Copper Finch', 'GoldenFinch'])
        self.assertEqual(list(result['accounts']), ['#P0'])
        self.assertEqual(result['accounts']['#P0']['player_name'], 'Golden Finch')
        self.assertEqual(len(result['name_resolutions'][0]['candidates']), 1)

    async def test_single_typo_and_transposition_can_resolve(self):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Cobalt'), ('#Q0', 'Velvet Quill')]))):
            result = await account_names.resolve_account_names(context_with_names(), ['Cobal', 'Vevlet Quill'])
        self.assertEqual(set(result['accounts']), {'#P0', '#Q0'})
        self.assertTrue(result['selection_complete'])

    async def test_extra_or_missing_segments_do_not_fuzzy_match(self):
        for query, name in (
                ('lumenbirch', 'Lumen.B'),
                ('Lumen Birch', 'Lumen B'),
                ('Amber Comet', 'Amber Red Comet'),
                ('Amber Red Comet', 'Amber Comet'),
                ('Amber A Comet', 'Amber Comet')):
            with self.subTest(query=query, name=name):
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([('#P0', name)]))):
                    result = await account_names.resolve_account_names(context_with_names(), [query])
                self.assertEqual(result['name_resolutions'][0]['status'], 'not_found')

    async def test_exact_then_prefix_precede_fuzzy_and_prefix_ambiguity_is_kept(self):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Cobalt'), ('#Q0', 'Cobalta'), ('#Y0', 'Cobalu')]))):
            result = await account_names.resolve_account_names(context_with_names(), ['Cobalt', 'Cobal'])
        self.assertEqual(list(result['accounts']), ['#P0'])
        self.assertEqual(result['name_resolutions'][0]['candidates'][0]['player_tag'], '#P0')
        self.assertEqual(result['name_resolutions'][1]['status'], 'ambiguous')
        self.assertEqual({row['player_tag'] for row in result['name_resolutions'][1]['candidates']},
                         {'#P0', '#Q0', '#Y0'})
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Cobalta'), ('#Q0', 'Cobalu')]))):
            result = await account_names.resolve_account_names(context_with_names(), ['Cobalt'])
        self.assertEqual(list(result['accounts']), ['#P0'])

    async def test_stored_candidates_are_filtered_by_family_on_each_table_and_alias(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'names.db')
            repository.initialize()
            repository.store_snapshots(1, [
                {'clan_code': 'BEH', 'player_tag': '#P0', 'player_name': 'Silver Kite'},
                {'clan_code': '#OUTSIDE', 'player_tag': '#Q0', 'player_name': 'Copper Finch'}])
            repository.store_final_war_rosters([
                {'war_id': '#P2', 'clan_code': 'BEH', 'player_tag': '#Y0',
                 'player_name': 'Golden Finch', 'roster_state': 'warEnded'},
                {'war_id': '#P2', 'clan_code': '#OUTSIDE', 'player_tag': '#L0',
                 'player_name': 'Velvet Quill', 'roster_state': 'warEnded'},
                {'war_id': '#P2', 'clan_code': '#OUTSIDE', 'player_tag': '#Y0',
                 'player_name': 'Outside Alias', 'roster_state': 'warEnded'}])
            context = replace(context_with_names(), cwl_queries=CwlQueries(repository, lambda: {}))
            self.assertEqual(context.cwl_queries.account_names(), [
                {'player_tag': '#P0', 'player_name': 'Silver Kite'},
                {'player_tag': '#Y0', 'player_name': 'Golden Finch'}])
            with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
                result = await account_names.resolve_account_names(context, [
                    'Silver Kite', 'Golden Finch', 'Copper Finch', 'Velvet Quill', 'Outside Alias'])
                for name in ('Copper Finch', 'Velvet Quill', 'Outside Alias'):
                    unified = await identity_reads.member_accounts(context, {'name': name})
                    self.assertEqual(unified['name_resolutions'][0]['status'], 'not_found')
                    self.assertEqual(unified['name_resolutions'][0]['matching_members'], [])
                    self.assertEqual(unified['name_resolutions'][0]['candidates'], [])
                    self.assertEqual(unified['rows'], [])
            self.assertEqual(set(result['accounts']), {'#P0', '#Y0'})
            self.assertEqual([row['status'] for row in result['name_resolutions']],
                             ['resolved', 'resolved', 'not_found', 'not_found', 'not_found'])

    async def test_ambiguous_owner_details_survive_report_and_writer_projection(self):
        context = context_with_names(linked=[('#P0', 'Silver Kite'), ('#Q0', 'Silver Kite')])
        context.account_links.get_all_links()['#Q0']['discord_user_id'] = 43
        owner = SimpleNamespace(id=42, display_name='Synthetic Pilot')
        other = SimpleNamespace(id=43, display_name='Synthetic Navigator')
        context.guild.get_member = lambda identifier: {42: owner, 43: other}.get(identifier, context.member)
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#Y0', 'Silver Kite')]))), patch.object(identity_reads, 'profiles', AsyncMock()) as profiles:
            result = await tool.handler(context, {'account_names': ['Silver Kite']})
            projected = await cohort_writer_evidence(context, {'owners': result})
        profiles.assert_not_called()
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['rows'], [])
        evidence = answer_evidence(projected, guild=context.guild)
        candidates = evidence['owners']['name_resolutions'][0]['candidates']
        self.assertEqual([(row['player_tag'], row['member_id'], row['member_name'], row['membership_status'])
                          for row in candidates], [
            ('#P0', 42, 'Synthetic Pilot', 'present'), ('#Q0', 43, 'Synthetic Navigator', 'present'),
            ('#Y0', None, None, 'unlinked')])
        retained = await tool.handler(context, {'report_id': result['report_id']})
        self.assertEqual(family_model_view(retained)['name_resolutions'], result['name_resolutions'])
        self.assertEqual(context.state.resolved_members, {})

    async def test_ambiguous_owners_distinguish_absent_member_and_unavailable_links(self):
        for linked, expected in ((True, 'not_in_guild'), (False, 'links_unavailable')):
            with self.subTest(linked=linked):
                context = context_with_names(linked=[('#P0', 'Silver Kite'), ('#Q0', 'Silver Kite')])
                if not linked:
                    context = replace(context, account_links=None)
                with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                        ('#P0', 'Silver Kite'), ('#Q0', 'Silver Kite')]))):
                    result = await identity_reads.member_accounts(context, {'account_names': ['Silver Kite']})
                for candidate in result['name_resolutions'][0]['candidates']:
                    self.assertEqual(candidate['membership_status'], expected)
                    self.assertEqual(candidate['member_id'], 42 if linked else None)
                    self.assertIsNone(candidate['member_name'])

    async def test_access_is_rechecked_after_ambiguous_owner_join_and_on_report_reuse(self):
        context = context_with_names(linked=[('#P0', 'Silver Kite'), ('#Q0', 'Silver Kite')])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
            result = await tool.handler(context, {'account_names': ['Silver Kite']})
        context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await tool.handler(context, {'report_id': result['report_id']})

        context = context_with_names(linked=[('#P0', 'Silver Kite'), ('#Q0', 'Silver Kite')])
        def lose_access(tags):
            context.member.roles = []
            return {tag: {'discord_user_id': 42} for tag in tags}
        context.account_links.get_links_by_tags = lose_access
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
            with self.assertRaises(AgentAccessLost):
                await tool.handler(context, {'account_names': ['Silver Kite']})
        self.assertEqual(context.state.reports, {})

    async def test_person_name_uses_both_sides_even_with_game_hints(self):
        context = context_with_names(linked=[('#P0', 'Silver Kite')])
        owner = SimpleNamespace(id=42, display_name='Lumen (Birch)', name='synthetic_pilot',
                                nick='Lumen (Birch)', roles=[], joined_at=None)
        context.guild.members = [owner]
        context.guild.get_member = lambda identifier: owner if identifier == 42 else context.member
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())) as source, \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            for name in ('Lumen Birch', 'Silver Kite'):
                result = await identity_reads.member_accounts(context, {'name': name})
                self.assertEqual(result['rows'][0]['member_id'], 42)
                self.assertEqual(result['rows'][0]['member_name'], 'Lumen (Birch)')
                self.assertEqual(result['name_resolutions'][0]['matching_members'][0]['member_id'], 42)
        self.assertEqual(source.await_count, 2)

    async def test_mixed_selectors_return_a_precise_correctable_error_without_reads(self):
        with patch.object(account_names, 'resolve_names', AsyncMock()) as resolve:
            for selector, value in (('name', 'Lumen Birch'), ('member_ids', [42]),
                                    ('account_tags', ['#P0']), ('cohort_ref', 'synthetic')):
                with self.subTest(selector=selector):
                    result = await identity_reads.member_accounts(context_with_names(), {
                        'account_names': ['Silver Kite'], selector: value})
                    self.assertEqual(result['cause'], 'invalid_arguments')
                    self.assertEqual(result['conflicting_fields'], ['account_names', selector])
                    self.assertIn('account_names cannot be combined with ' + selector, result['error'])
                    self.assertIn('separate plan steps', result['error'])
        resolve.assert_not_called()

    def test_planner_catalogue_uses_one_lookup_and_exposes_both_sides(self):
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        catalogue = capability_list({tool.definition.name: tool}, include_returns=False)
        for text in (FAMILY_INSTRUCTION, catalogue):
            self.assertIn('Discord ID', text)
            self.assertIn('account_names', text)
            self.assertIn('alternatives', text.lower())
            self.assertNotIn('Stats names use account_names unless', text)
        properties = tool.definition.parameters['properties']
        self.assertIn('matching_members', properties['name']['description'])
        self.assertIn('same lookup', properties['account_names']['description'])
        self.assertIn('matching_members', catalogue)
        self.assertIn('member_name,membership_status', catalogue)

    async def test_missing_roster_is_unknown_instead_of_a_false_unmatched_account(self):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value={
                'error': 'Synthetic read failure', 'cause': 'source_unavailable'})):
            result = await account_names.resolve_account_names(context_with_names(), ['Violet Orbit'])
        self.assertEqual(result['name_resolutions'][0]['status'], 'unknown')
        self.assertFalse(result['selection_complete'])
        self.assertEqual(len(result['missing_sources']), len(CLANS))

    async def test_complete_empty_lookup_differs_from_ambiguous_and_failed_sources(self):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())):
            result = await account_names.resolve_account_names(context_with_names(), ['Violet Orbit'])
        self.assertEqual(result['name_resolutions'][0]['status'], 'not_found')
        self.assertEqual(result['missing_sources'], [])

    async def test_dated_account_read_uses_real_stored_reg_and_cwl_records_and_ranked_query(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'stats.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for identity, kind, end, expected, used in (
                        ('#P2', 'REG', '2025-12-01', 2, 1),
                        ('#P8', 'CWL', '2025-12-02', 1, 1),
                        ('#P9', 'REG', '2025-10-01', 2, 0)):
                    timestamp = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                        state,preparation_start_ts,start_ts,end_ts,last_seen_ts,team_size,attacks_per_member)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, 'BEH', CLANS['BEH'].tag, 'warEnded', timestamp-100,
                         timestamp-50, timestamp, timestamp, 2, expected))
                    for tag, name in (('#P0', 'Amber Comet'), ('#Q0', 'Other Beacon')):
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,
                            player_tag,player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''',
                            (identity, 'BEH', tag, name, expected, used, 'warEnded', timestamp))
            cwl = CwlQueries(repository, lambda: {})
            self.assertEqual({row['player_tag'] for row in cwl.account_names()}, {'#P0', '#Q0'})
            cwl.attendance = Mock(wraps=cwl.attendance)
            ranked = Mock(return_value={'accounts': [], 'stored_battles': [], 'total_accounts': 0,
                'next_offset': None, 'complete': True, 'coverage_complete': False,
                'unobserved_selections': ['#P0'], 'limitations': []})
            context = replace(context_with_names(linked=[('#P0', 'Amber Comet'), ('#Q0', 'Other Beacon')]),
                cwl_queries=cwl, ranked_queries=SimpleNamespace(window=ranked))
            tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
            arguments = {'account_names': ['ambercomet'], 'after': '2025-11-02T00:00:00+00:00',
                         'before': '2026-01-01T00:00:00+00:00'}
            registry = {tool.definition.name: tool}
            plan = {'goal': 'Read selected account activity', 'effort': 'high', 'output': 'text',
                'finish_after_reads': True, 'steps': [{'id': 'stats', 'capability': tool.definition.name,
                'arguments': arguments, 'depends_on': []}]}
            self.assertTrue(check_plan(plan, registry).ok)
            self.assertIn('account_names', capability_list(registry))
            live_logs = {'items': [{'tag': '#P0', 'status': 'ok', 'data': {'items': []}}]}
            with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
                 patch.object(identity_reads, 'profiles', AsyncMock(return_value={
                     '#P0': {'status': 'ok', 'data': {'name': 'Amber Comet', 'warStars': 9876}}})), \
                 patch('elbow_helper.features.agent.datasets.ranked._live_batch', AsyncMock(return_value=live_logs)):
                result = await tool.handler(context, arguments)
                projected = await cohort_writer_evidence(context, {'stats': result})
            self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
            activity = result['rows'][0]['activity']
            self.assertEqual(activity['regular_war']['war_ids'], ['#P2'])
            self.assertEqual((activity['regular_war']['attacks_used'], activity['regular_war']['missed']), (1, 1))
            self.assertEqual(activity['cwl']['war_ids'], ['#P8'])
            self.assertEqual(activity['cwl']['attacks_used'], 1)
            self.assertEqual({call.kwargs['war_type'] for call in cwl.attendance.call_args_list}, {'REG', 'CWL'})
            for call in cwl.attendance.call_args_list:
                self.assertEqual(set(call.kwargs['clan_codes']), set(CLANS))
                self.assertEqual(call.kwargs['after_ts'], int(datetime(2025, 11, 2, tzinfo=timezone.utc).timestamp()))
            self.assertEqual(ranked.call_args.kwargs['values'], ('#P0',))
            self.assertEqual(ranked.call_args.kwargs['selector'], 'account_tags')
            self.assertEqual(set(result['history_reads']), set(account_activity.HISTORY_KINDS))
            self.assertEqual(projected['stats']['rows'][0]['activity'], activity)
            self.assertEqual(await cohort_selection(context, result['report_id'], 'account_tags'),
                             {'account_tags': ['#P0']})
            self.assertTrue(AnswerFlow._planned_reads_complete(SimpleNamespace(plan=plan), {'stats': result}))
            profile_only = {key: value for key, value in result.items() if key != 'history_reads'}
            self.assertFalse(AnswerFlow._planned_reads_complete(SimpleNamespace(plan=plan), {'stats': profile_only}))

    async def test_failed_history_keeps_other_observations_and_empty_is_not_zero(self):
        from elbow_helper.features.agent.families import ranked_reads
        rows = [{'player_tag': '#P0'}]
        read_wars = AsyncMock(side_effect=[
            {'rows': [{'player_tag': '#P0', 'war_ids': [], 'attacks_used': None,
                       'observation_status': 'no_stored_participation'}]},
            {'error': 'Synthetic CWL failure', 'cause': 'source_unavailable'}])
        with patch.object(account_activity, 'attendance', read_wars), \
             patch.object(ranked_reads, 'ranked_activity', AsyncMock(return_value={
                 'rows': [{'player_tag': '#P0', 'attacks_used': 4}]})):
            reads = await account_activity.account_activity(_context(), rows,
                {'after': '2025-11-02T00:00:00+00:00'})
        self.assertEqual(reads['cwl']['status'], 'failed')
        self.assertTrue(all(row['read_ran'] for row in reads.values()))
        self.assertEqual(rows[0]['activity']['regular_war']['status'], 'empty')
        self.assertIsNone(rows[0]['activity']['regular_war']['attacks_used'])
        self.assertEqual(rows[0]['activity']['ranked']['attacks_used'], 4)
        self.assertEqual(read_wars.call_args.args[1]['before'], '2026-01-01T00:00:00+00:00')

    async def test_bad_period_is_rejected_before_account_or_history_reads(self):
        with patch.object(account_names, 'resolve_account_names', AsyncMock()) as resolve:
            result = await identity_reads.member_accounts(_context(), {'account_names': ['Amber Comet'],
                'after': '2026-02-01T00:00:00+00:00'})
        self.assertEqual(result['cause'], 'invalid_arguments')
        resolve.assert_not_called()

    async def test_rejected_history_selection_does_not_establish_missing_data(self):
        from elbow_helper.features.agent.families import ranked_reads
        with patch.object(account_activity, 'attendance', AsyncMock(return_value={'rows': []})), \
             patch.object(ranked_reads, 'ranked_activity', AsyncMock(return_value={
                 'error': 'Synthetic selection error', 'cause': 'invalid_arguments'})):
            reads = await account_activity.account_activity(_context(), [{'player_tag': '#P0'}],
                {'after': '2025-11-02T00:00:00+00:00'})
        self.assertFalse(reads['ranked']['read_ran'])
        self.assertEqual(reads['ranked']['status'], 'invalid_selection')
