"""Synthetic named-account resolution and dated activity through retained reports."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.features.agent.engine.flow import AnswerFlow
from elbow_helper.features.agent.families import account_names, identity_reads
from elbow_helper.features.agent.families import account_activity
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
        get_links_for_members=Mock(side_effect=AssertionError('Do not expand an account owner'))),
        cwl_queries=SimpleNamespace(account_names=lambda: [
            {'player_tag': tag, 'player_name': name} for tag, name in stored]))
    context.guild.get_member = lambda identifier: context.member if identifier == context.member.id else None
    return context


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
                {'account_names': [' aMbErCoMet ', 'Nebous']})
        self.assertEqual([row['status'] for row in result['name_resolutions']], ['resolved', 'resolved'])
        self.assertEqual({row['player_tag'] for row in result['rows']}, {'#P0', '#Y0'})
        self.assertEqual(profiles.call_args.args[1], ['#P0', '#Y0'])
        self.assertTrue(result['selection_complete'])
        self.assertIsNone(next(row for row in result['rows'] if row['player_tag'] == '#Y0')['member_id'])
        context.account_links.get_links_for_members.assert_not_called()

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

    async def test_short_dropped_syllable_and_transposition_can_resolve(self):
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters([
                ('#P0', 'Cobalt'), ('#Q0', 'Velvet Quill')]))):
            result = await account_names.resolve_account_names(context_with_names(), ['Cobt', 'Vevlet Quill'])
        self.assertEqual(set(result['accounts']), {'#P0', '#Q0'})
        self.assertTrue(result['selection_complete'])

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
