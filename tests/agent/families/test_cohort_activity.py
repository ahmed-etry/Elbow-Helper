"""Activity joins preserve the full roster and never classify message content."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.families.cohort_activity import roster_cohort
from elbow_helper.agent.families.cohort_activity import chat_projection
from elbow_helper.agent.families.cohort_activity import war_measures
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.families.identity_reads import TOOLS
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.checker import check_plan
from agent.engine.test_agent_plan_flow import _context


class MetadataOnly:
    channel_id = 91
    author_id = 42
    timestamp = '2025-12-15T00:00:00+00:00'
    message_id = discord.utils.time_snowflake(datetime(2025, 12, 15, tzinfo=timezone.utc))

    @property
    def content(self):
        raise AssertionError('Member-written content must not be inspected')


class CohortActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_completion_uses_confirmed_attack_opportunities_and_preserves_unknowns(self):
        source = [dict(player_tag='#P0', player_name='First', war_id='R1',
                       attacks_used=1, attacks_expected=2, missed=1),
                  dict(player_tag='#P1', player_name='Second', war_id='C1',
                       attacks_used=1, attacks_expected=1, missed=0)]
        observed = aggregate_attendance(source, {}, 'account')
        result = war_measures(observed)
        self.assertEqual((result['attacks_used'], result['attacks_expected'],
                          result['missed'], result['attack_completion_percent']), (2, 3, 1, 66.67))
        zero = aggregate_attendance([dict(source[0], attacks_used=0, missed=2)], {}, 'account')
        self.assertEqual(war_measures(zero)['attack_completion_percent'], 0)
        incomplete = aggregate_attendance([dict(source[0], attacks_expected=None, missed=None)], {}, 'account')
        for observations in ([], incomplete, observed + incomplete):
            with self.subTest(observations=observations):
                self.assertIsNone(war_measures(observations)['attack_completion_percent'])
                self.assertIsNone(war_measures(observations)['attacks_expected'])
        self.assertEqual(war_measures([], unavailable=True)['status'], 'source_unavailable')
        self.assertIsNone(war_measures([], unavailable=True)['wars_observed'])

    async def test_partial_channel_pages_keep_their_cursor_and_observations(self):
        import asyncio
        import time
        context = _context()
        rows = [{'member_id': 42}]
        first_page = SimpleNamespace(messages=(MetadataOnly(),),
            next_before_id=MetadataOnly.message_id, reached_window_start=False)
        async def history(**kwargs):
            if kwargs['before_id'] > MetadataOnly.message_id:
                return first_page
            await asyncio.Event().wait()
        context = replace(context, message_search=SimpleNamespace(history_page=AsyncMock(side_effect=history)))
        selection = {'after': '2025-12-01T00:00:00+00:00', 'before': '2026-01-01T00:00:00+00:00'}
        with patch('elbow_helper.agent.families.cohort_activity.searchable_channel_ids', AsyncMock(return_value=(91,))):
            partial = await chat_projection(context, rows, selection, deadline=time.monotonic()+0.01)
        self.assertFalse(partial['source_reads_complete'])
        self.assertEqual(partial['channel_cursors']['91'], MetadataOnly.message_id)
        self.assertEqual(rows[0]['chat_activity']['latest_message_id'], MetadataOnly.message_id)
        context.message_search.history_page = AsyncMock(return_value=SimpleNamespace(
            messages=(), next_before_id=None, reached_window_start=True))
        complete = await chat_projection(context, rows, selection, progress=partial)
        self.assertTrue(complete['source_reads_complete'])
        self.assertEqual(context.message_search.history_page.call_args.kwargs['before_id'], MetadataOnly.message_id)
        self.assertEqual(rows[0]['chat_activity']['latest_message_id'], MetadataOnly.message_id)

    def test_initial_catalogue_can_collect_and_resume_a_full_cohort(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        plan = {'goal': 'Read member activity across rosters', 'effort': 'high', 'output': 'text', 'steps': [
            {'id': 'cohort', 'capability': 'read_member_cohort', 'arguments': {'basis': 'clan_rosters',
                'include_activity': True, 'after': '2025-12-01T00:00:00+00:00', 'view': 'count'}, 'depends_on': []},
            {'id': 'resume', 'capability': 'read_member_cohort', 'arguments': {'basis': 'clan_rosters',
                'activity_ref': {'step': 'cohort', 'path': ['report_id']}, 'view': 'count'}, 'depends_on': ['cohort']}]}
        checked = check_plan(plan, registry)
        self.assertTrue(checked.ok, checked.error)

    async def test_timeout_continues_the_same_immutable_cohort_without_repeating_finished_reads(self):
        import asyncio
        context = _context()
        owner = SimpleNamespace(id=42, display_name='Member', roles=[], joined_at=None)
        context.guild.get_member = lambda identifier: owner if identifier == 42 else context.member
        roster = {'rows': [dict(clan_code='BES', player_tag='#P0', player_name='Account', donations=1)],
                  'count_complete': True, 'clans': []}
        context = replace(context, account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
            '#P0': {'discord_user_id': 42}}), message_search=SimpleNamespace(history_page=AsyncMock()))
        async def stalled(**kwargs):
            await asyncio.Event().wait()
        context.message_search.history_page = AsyncMock(side_effect=stalled)
        tool = next(t for t in TOOLS if t.definition.name == 'read_member_cohort')
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock(return_value={'rows': []})), \
             patch('elbow_helper.agent.families.cohort_activity.searchable_channel_ids', AsyncMock(return_value=(91,))), \
             patch('elbow_helper.agent.families.cohort_activity.TOOL_TIMEOUT_SECONDS', 2):
            first = await tool.handler(context, {'basis': 'clan_rosters', 'include_activity': True,
                'after': '2025-12-01T00:00:00+00:00', 'view': 'count'})
        self.assertEqual(first['activity_coverage']['chat_coverage']['members_not_checked'], 1)
        retained = context.state.reports[first['report_id']].payload
        context.message_search.history_page = AsyncMock(return_value=SimpleNamespace(messages=(MetadataOnly(),), next_before_id=None, reached_window_start=True))
        second = await tool.handler(context, {'basis': 'clan_rosters', 'activity_ref': first['report_id']})
        self.assertTrue(second['activity_coverage']['chat_coverage']['source_reads_complete'])
        self.assertEqual(second['rows'][0]['chat_activity']['status'], 'observed')
        self.assertEqual(context.state.reports[first['report_id']].payload, retained)
        third = await tool.handler(context, {'basis': 'clan_rosters', 'activity_ref': second['report_id']})
        self.assertEqual(context.message_search.history_page.await_count, 1)
        self.assertEqual(second['rows'], third['rows'])

    async def test_accounts_join_across_clans_and_unlinked_accounts_survive(self):
        context = _context()
        owner = SimpleNamespace(id=42, display_name='Another member', roles=[], joined_at=None)
        context.guild.get_member = lambda identifier: owner if identifier == 42 else context.member
        roster = {'rows': [dict(clan_code=code, player_tag=tag, player_name=tag,
            donations=donations, townHallLevel=17, profile_status='ok')
            for code, tag, donations in [('BES', '#P0', 12), ('BEM', '#P2', 3), ('BEM', '#P8', None)]],
            'count_complete': True, 'clans': []}
        links = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {'discord_user_id': 42} for tag in ('#P0', '#P2')})
        search = SimpleNamespace(history_page=AsyncMock(return_value=SimpleNamespace(
            messages=(MetadataOnly(),), next_before_id=None, reached_window_start=True)))
        context = replace(context, account_links=links, message_search=search)
        war_rows = [dict(player_tag='#P0', player_name='Account', war_id='W1',
                        attacks_used=2, attacks_expected=2, missed=0),
                    dict(player_tag='#P0', player_name='Account', war_id='W2',
                         attacks_used=0, attacks_expected=1, missed=1)]
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock(return_value={'rows': war_rows, 'coverage': {'historical_collection_complete': False}})), \
             patch('elbow_helper.agent.families.cohort_activity.searchable_channel_ids', AsyncMock(return_value=(91,))):
            result = await roster_cohort(context, {'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'})
        self.assertEqual(result['account_count'], 3)
        self.assertEqual(result['member_count'], 1)
        self.assertEqual(len(result['rows']), 2)
        member, unlinked = result['rows']
        self.assertEqual(member['member_id'], 42)
        self.assertEqual(member['donations'], 15)
        self.assertEqual(member['war_activity']['attacks_used'], 2)
        self.assertEqual(member['war_activity']['wars_observed'], 2)
        self.assertEqual(member['war_activity']['attacks_expected'], 3)
        self.assertEqual(member['war_activity']['attack_completion_percent'], 66.67)
        self.assertEqual(member['chat_activity']['status'], 'observed')
        self.assertIsNone(unlinked['donations'])
        self.assertIsNone(unlinked['war_activity']['attacks_used'])
        self.assertEqual(unlinked['chat_activity']['status'], 'unlinked')
        self.assertEqual(search.history_page.await_count, 1)
        self.assertEqual(search.history_page.call_args.kwargs['channel_id'], 91)
        visible = family_model_view({**result, 'family': 'read_member_cohort'})
        self.assertEqual({tag for r in visible['rows'] for tag in r['account_tags']}, {'#P0', '#P2', '#P8'})
        self.assertEqual(visible['rows'][0]['donations'], 15)
        accounts = {account['player_tag']: account for account in visible['rows'][0]['accounts']}
        self.assertEqual(accounts['#P0']['war_activity']['attacks_used'], 2)
        self.assertEqual(accounts['#P0']['war_activity']['attacks_expected'], 3)
        self.assertEqual(accounts['#P0']['war_activity']['missed'], 1)
        self.assertEqual(accounts['#P0']['war_activity']['attack_completion_percent'], 66.67)
        self.assertEqual(visible['rows'][0]['attack_completion_percent'], 66.67)
        self.assertIsNone(accounts['#P2']['war_activity']['attacks_used'])
        self.assertIsNone(accounts['#P2']['war_activity']['attack_completion_percent'])
        self.assertEqual(accounts['#P2']['war_activity']['status'], 'no_stored_participation')
        by_clan = {row['clan_code']: row for row in result['clan_activity']}
        self.assertEqual(by_clan['BES']['accounts_with_observed_attacks'], 1)
        self.assertEqual(by_clan['BEM']['accounts_with_observed_attacks'], 0)
        self.assertEqual(by_clan['BEM']['accounts_without_stored_participation'], 2)

    async def test_invalid_window_is_rejected_before_activity_queries(self):
        context = _context()
        roster = {'rows': [], 'count_complete': True, 'clans': []}
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock()) as wars:
            missing = await roster_cohort(context, {'include_activity': True})
            invalid = await roster_cohort(context, {'include_activity': True, 'after': '2026-02-01T00:00:00+00:00'})
        self.assertEqual(missing['cause'], 'needs_input')
        self.assertEqual(invalid['cause'], 'invalid_arguments')
        wars.assert_not_awaited()
