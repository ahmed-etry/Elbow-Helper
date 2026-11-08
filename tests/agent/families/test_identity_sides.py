"""Planner-selected name sources fall back only after a miss."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import account_names, identity_reads
from elbow_helper.agent.tool_support.model_view import family_model_view
from elbow_helper.agent.engine.result_hints import RESULT_FIELDS
from elbow_helper.configuration.clans import CLANS
from agent.families.test_named_account_stats import lookup_context, named_member, rosters


class IdentitySideTests(unittest.IsolatedAsyncioTestCase):
    async def read_names(self, context, arguments, *, roster=()):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters(roster))), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            return await identity_reads.member_accounts(context, arguments)

    async def test_game_hit_never_searches_discord_or_expands_unrelated_alts(self):
        context = lookup_context([named_member(42, 'Different Pilot'), named_member(43, 'Silver Kite')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Other Beacon', 42)])
        with patch.object(account_names, 'matching_members', AsyncMock()) as discord:
            result = await self.read_names(context, {'name': 'Silver Kite', 'look_in': 'game'})
        discord.assert_not_awaited()
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        self.assertEqual([row['member_id'] for row in result['members']], [42])
        self.assertEqual(result['name_resolutions'][0]['found_in'], 'game')
        self.assertEqual(context.state.resolved_members, {42: 'Different Pilot'})

    async def test_game_miss_falls_back_to_discord_and_joins_accounts(self):
        context = lookup_context([named_member(42, 'Lumen [Birch]')],
                                 [('#P0', 'Other Beacon', 42)])
        for selector in ('name', 'account_names'):
            result = await self.read_names(context, {
                selector: 'Birch' if selector == 'name' else ['Birch'], 'look_in': 'game'})
            self.assertEqual(result['status'], 'resolved')
            self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
            view = family_model_view({**result, 'family': 'read_member_accounts'})
            resolution = view['name_resolutions'][0]
            self.assertEqual((resolution['look_in'], resolution['found_in']), ('game', 'discord'))

    async def test_discord_miss_falls_back_to_game_and_joins_owner(self):
        context = lookup_context([named_member(42, 'Different Pilot')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Other Beacon', 42)])
        result = await self.read_names(context, {'account_names': ['Silver Kte'], 'look_in': 'discord'})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        self.assertEqual([row['member_id'] for row in result['members']], [42])
        self.assertEqual(result['name_resolutions'][0]['found_in'], 'game')
        self.assertEqual(result['name_resolutions'][0]['matching_members'], [])

    async def test_discord_hit_without_accounts_returns_member_and_never_searches_game(self):
        context = lookup_context([named_member(42, 'Silver Kite'), named_member(43, 'Different Pilot')],
                                 [('#P0', 'Silver Kite', 43)])
        with patch.object(account_names, 'resolve_account_names', AsyncMock()) as game:
            result = await self.read_names(context, {'name': 'Silver Kite', 'look_in': 'discord'})
        game.assert_not_awaited()
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual([(row['member_id'], row['player_tag']) for row in result['rows']], [(42, None)])
        self.assertEqual(result['members'][0]['account_tags'], [])
        resolution = family_model_view(result)['name_resolutions'][0]
        self.assertEqual(resolution['matching_members'][0]['account_links_status'], 'no_linked_accounts')
        self.assertEqual(context.state.resolved_members, {42: 'Silver Kite'})

    async def test_discord_hit_without_link_setup_preserves_unknown(self):
        context = replace(lookup_context([named_member(42, 'Silver Kite')]), account_links=None)
        with patch.object(account_names, 'resolve_account_names', AsyncMock()) as game:
            result = await self.read_names(context, {'name': 'Silver Kite', 'look_in': 'discord'})
        game.assert_not_awaited()
        self.assertFalse(result['count_complete'])
        resolution = family_model_view(result)['name_resolutions'][0]
        self.assertEqual(resolution['matching_members'][0]['account_links_status'], 'links_unavailable')

    async def test_game_ambiguity_lists_owners_and_never_searches_discord(self):
        context = lookup_context([named_member(42, 'Silver Kite'), named_member(43, 'Other Pilot')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Silver Kite', 43)])
        with patch.object(account_names, 'matching_members', AsyncMock()) as discord:
            result = await self.read_names(context, {'name': 'Silver Kite', 'look_in': 'game'})
        discord.assert_not_awaited()
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0', '#Q0'])
        self.assertTrue(all(row['candidate_for'] == ['Silver Kite'] for row in result['rows']))
        self.assertEqual(result['resolved_member_ids'], [])
        resolution = family_model_view(result)['name_resolutions'][0]
        self.assertEqual([(row['player_tag'], row['member_id']) for row in resolution['candidates']],
                         [('#P0', 42), ('#Q0', 43)])
        self.assertEqual(context.state.resolved_members, {})


    async def test_discord_ambiguity_lists_account_names_and_tags_without_game_search(self):
        context = lookup_context([named_member(42, 'Silver Kite'),
                                 named_member(43, 'Silver Kite Navigator'), named_member(44, 'Silver Kite')],
                                 [('#P0', 'Amber Comet', 42), ('#Q0', 'Other Beacon', 43)])
        with patch.object(account_names, 'resolve_account_names', AsyncMock()) as game:
            result = await self.read_names(context, {'name': 'Silver Kite', 'look_in': 'discord'})
        game.assert_not_awaited()
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual([row['member_id'] for row in result['members']], [42, 43, 44])
        self.assertTrue(all(row['candidate_for'] == ['Silver Kite'] for row in result['members']))
        self.assertEqual(result['resolved_member_ids'], [])
        resolution = family_model_view(result)['name_resolutions'][0]
        self.assertEqual([row['accounts'] for row in resolution['matching_members']], [
            [{'player_tag': '#P0', 'player_name': 'Amber Comet'}],
            [], [{'player_tag': '#Q0', 'player_name': 'Other Beacon'}]])
        self.assertEqual(resolution['matching_members'][1]['account_links_status'], 'no_linked_accounts')
        self.assertEqual(context.state.resolved_members, {})


    async def test_unknown_first_source_is_not_erased_by_complete_fallback_miss(self):
        for look_in in ('game', 'discord'):
            context = lookup_context()
            response = rosters()
            if look_in == 'game':
                response = {'error': 'Synthetic unavailable', 'cause': 'source_unavailable'}
            else:
                context.guild.chunked = False
                context.guild.chunk = AsyncMock()
            with patch.object(account_names, 'read_clash', AsyncMock(return_value=response)):
                result = await identity_reads.member_accounts(context, {
                    'name': 'Absent Subject', 'look_in': look_in})
            self.assertEqual(result['name_resolutions'][0]['status'], 'unknown')
            self.assertTrue(result['missing_sources'])

    async def test_multiple_queries_fall_back_only_for_misses_and_deduplicate_accounts(self):
        context = lookup_context([named_member(42, 'Silver Kite')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Other Beacon', 42)])
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(account_names, 'matching_members', wraps=account_names.matching_members) as discord:
            result = await account_names.resolve_names(context, ['Silver Kite', 'synthetic_42'], 'game')
        self.assertEqual(set(result['accounts']), {'#P0', '#Q0'})
        self.assertEqual([row['found_in'] for row in result['name_resolutions']], ['game', 'discord'])
        self.assertEqual(discord.await_args.args[1], 'synthetic_42')
        discord.assert_awaited_once()

    async def test_one_unselected_name_keeps_the_read_needs_input(self):
        context = lookup_context([named_member(42, 'Silver Kite')], [('#P0', 'Silver Kite', 42)])
        result = await self.read_names(context, {
            'account_names': ['Silver Kite', 'Absent Subject'], 'look_in': 'game'})
        self.assertEqual([row['status'] for row in result['name_resolutions']], ['resolved', 'not_found'])
        self.assertFalse(result['selection_complete'])
        self.assertEqual(result['status'], 'needs_input')


    async def test_profile_is_once_per_owner_in_retained_and_model_views(self):
        owner = named_member(42, 'Silver Kite', nick='Silver Kite')
        owner.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        owner.roles = [SimpleNamespace(id=CLANS['BES'].member_role_id, name='Synthetic home',
                                       is_default=lambda: False)]
        context = lookup_context([owner], [('#P0', 'Silver Kite', 42), ('#Q0', 'Other Beacon', 42)])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            result = await tool.handler(context, {'name': 'Silver Kite', 'look_in': 'discord',
                                                  'include_roles': True, 'include_profile': True})
        retained = await tool.handler(context, {'report_id': result['report_id']})
        for data in (result, retained, family_model_view(retained)):
            self.assertEqual(len(data['members']), 1)
            profile = data['members'][0]
            self.assertEqual(profile['member_id'], 42)
            self.assertEqual(profile['account_tags'], ['#P0', '#Q0'])
            self.assertEqual(profile['nickname'], 'Silver Kite')
            self.assertEqual(profile['joined_at'], owner.joined_at.isoformat())
            self.assertEqual(profile['home_clan_status'], 'resolved')
            self.assertEqual(profile['roles'][0]['name'], 'Synthetic home')
            for account in data['rows']:
                self.assertEqual(account['member_id'], 42)
                for field in ('roles', 'nickname', 'joined_at', 'leadership', 'home_clans',
                              'home_clan_basis', 'participation_clans', 'family_membership_roles'):
                    self.assertNotIn(field, account)
        self.assertTrue(all('selected_by' not in row for row in result['rows']))
        page = family_model_view(await tool.handler(context, {'report_id': result['report_id'], 'limit': 1}))
        self.assertEqual(page['members'][0]['account_tags'], ['#P0', '#Q0'])
        self.assertIn('roles', RESULT_FIELDS['read_member_accounts']['members'][0])
        self.assertNotIn('roles', RESULT_FIELDS['read_member_accounts']['rows'][0])

    async def test_missing_links_and_departed_members_keep_one_unknown_profile(self):
        owner = named_member(42, 'Synthetic Pilot')
        context = replace(lookup_context([owner]), account_links=None)
        result = await identity_reads.member_accounts(context, {'member_ids': [42]})
        self.assertEqual(len(result['members']), 1)
        self.assertEqual(result['members'][0]['account_links_status'], 'links_unavailable')
        self.assertIsNone(result['members'][0]['has_elbow_helper_links'])
        self.assertFalse(result['count_complete'])
        context = lookup_context([], [('#P0', 'Silver Kite', 404)])
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            result = await identity_reads.member_accounts(context, {'member_ids': [404]})
        self.assertEqual(result['members'][0]['membership_status'], 'not_in_guild')
        self.assertNotIn('leadership', result['members'][0])
        self.assertEqual(result['members'][0]['account_tags'], ['#P0'])
