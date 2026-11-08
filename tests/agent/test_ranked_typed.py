"""Checked ranked measures, coverage, selectors and failures with forbidden network clients."""

import json
import sqlite3
import unittest

from agent import test_ranked_contracts as contracts
from agent.families.test_ranked import account, stored, BATTLE, AFTER, BEFORE
from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.tool_support.core import FamilyReport
from elbow_helper.agent.reads.ranked import read_ranked
from elbow_helper.agent.reads.ranked_tools import TOOLS
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError


class RankedTypedTests(unittest.IsolatedAsyncioTestCase):
    def context(self, source=None):
        return contracts.RankedContracts().context(source)

    def arguments(self, **kwargs):
        return {'member_ids': [41], 'after': AFTER, 'before': BEFORE, **kwargs}

    async def test_cohort_ratios_weight_all_battles_before_paging(self):
        context, source, forbidden = self.context(stored([
            account('#P0', battles=[dict(BATTLE, attack=False, stars=stars, destructionPercentage=destruction)
                                    for stars, destruction in [(3, 100), (2, 80), (1, 60)]]),
            account('#Q0', battles=[dict(BATTLE, attack=False, battleType='LEGEND', stars=0, destructionPercentage=40)]),
            account('#P8', status='never_read')]))
        result = await TOOLS[0].handler(context, self.arguments(metric='average_stars', direction='defenses', limit=1))
        aggregate = result['aggregate']
        self.assertEqual(aggregate['battles'], 4)
        self.assertEqual(aggregate['average_stars'], {
            'numerator': 6, 'denominator': 4, 'value': 1.5, 'display': '1.5000'})
        self.assertEqual(aggregate['triple_rate_percent']['value'], 25)
        self.assertEqual(aggregate['average_destruction_percent']['value'], 70)
        self.assertEqual(aggregate['coverage']['requested_accounts'], 3)
        self.assertEqual(aggregate['coverage']['unavailable_tags'], ['#P8'])
        self.assertEqual(result['measurement']['field'], 'aggregate.average_stars')
        page = await TOOLS[0].handler(context, {'report_id': result['report_id'], 'cursor': result['next_cursor']})
        self.assertEqual(page['aggregate'], aggregate)
        source.assert_awaited_once()
        forbidden.assert_not_awaited()

    async def test_typed_modes_select_exact_counts_daily_values_and_details(self):
        for mode, attacks, stars in [('ranked_and_legend', 3, 5), ('ranked', 2, 3), ('legend', 1, 2)]:
            with self.subTest(mode=mode):
                context, _, forbidden = self.context()
                result = await read_ranked(context, self.arguments(mode=mode, direction='attacks',
                                                                  metric='average_stars', detail='detail'))
                self.assertEqual((result['aggregate']['battles'], result['aggregate']['stars_total']), (attacks, stars))
                self.assertEqual(result['resolved_selection']['mode'], mode)
                row = result['rows'][0]
                self.assertEqual(row['attacks_used'], attacks)
                self.assertTrue(all(b['payload']['battleType'].upper() in
                    (('RANKED', 'LEGEND') if mode == 'ranked_and_legend' else (mode.upper(),)) for b in row['battles']))
                if mode != 'ranked_and_legend':
                    self.assertEqual(row['daily'][0]['attacks_used'], attacks)
                self.assertIsNone(row['attacks_available'])
                self.assertFalse(row['counts_complete'])
                forbidden.assert_not_awaited()


    def cohort(self, context, tags, *, guild=None, requester=None):
        report = FamilyReport('cohort', guild or context.guild.id, 'read_member_cohort', json.dumps({
            'rows': [{'accounts': [{'player_tag': tag} for tag in tags]}],
            'summary': {'requester_id': requester or context.member.id}}))
        context.state.reports[report.report_id] = report
        context.state.report_sources[report.report_id] = frozenset()
        context.state.report_access_requirements[report.report_id] = frozenset()
        return report.report_id

    async def test_retained_cohort_over_two_hundred_accounts_is_not_a_model_page(self):
        alphabet = '0289PYLQGRJCUV'
        tags = ['#P' + alphabet[i // 196] + alphabet[i // 14 % 14] + alphabet[i % 14] for i in range(201)]
        context, source, forbidden = self.context(stored([account(tag, battles=[BATTLE]) for tag in tags]))
        identifier = self.cohort(context, tags)
        result = await TOOLS[0].handler(context, {'cohort_ref': identifier, 'after': AFTER, 'before': BEFORE,
                                                'direction': 'attacks', 'metric': 'average_stars', 'limit': 5})
        self.assertEqual(result['total_rows'], 201)
        self.assertEqual(result['aggregate']['battles'], 201)
        self.assertEqual(len(source.call_args.args[1]), 201)
        self.assertEqual(len(json.loads(context.state.reports[result['report_id']].payload)['rows']), 201)
        forbidden.assert_not_awaited()

    async def test_unavailable_wrong_guild_wrong_requester_and_revoked_cohorts_disclose_nothing(self):
        for variant in ('missing', 'wrong_guild', 'wrong_requester', 'revoked'):
            with self.subTest(variant=variant):
                context, source, forbidden = self.context()
                identifier = 'missing' if variant == 'missing' else self.cohort(context, ['#P0'],
                    guild=999 if variant == 'wrong_guild' else None,
                    requester=999 if variant == 'wrong_requester' else None)
                if variant == 'revoked':
                    context.member.roles.clear()
                    with self.assertRaises(AgentAccessLost):
                        await read_ranked(context, {'cohort_ref': identifier, 'after': AFTER, 'before': BEFORE})
                else:
                    result = await read_ranked(context, {'cohort_ref': identifier, 'after': AFTER, 'before': BEFORE})
                    self.assertEqual(result['cause'], 'report_unavailable')
                source.assert_not_awaited()
                forbidden.assert_not_awaited()

    async def test_access_revoked_during_a_read_prevents_disclosure(self):
        context, source, _ = self.context()
        async def revoke(*args, **kwargs):
            context.member.roles.clear()
            return contracts.fixture()
        source.side_effect = revoke
        with self.assertRaises(AgentAccessLost):
            await read_ranked(context, self.arguments())

    async def test_hibernation_clips_each_accounts_window_without_turning_an_unread_tail_into_zero(self):
        from datetime import datetime
        from agent.families.test_ranked import DAY_TWO
        context, source, _ = self.context(stored([
            account('#P0', battles=[BATTLE], status='never_read'),
            account('#Q0', battles=[dict(BATTLE, attack=False)], status='never_read')]))
        start = DAY_TWO / 1_000_000
        result = await read_ranked(context, {'account_tags': ['#P0', '#Q0'], 'after': AFTER, 'before': BEFORE},
                                   starts={'#P0': start + 1, '#Q0': start})
        self.assertTrue(all(row['activity_since']['observed_ranked_attacks_since_start'] is None for row in result['rows']))
        self.assertEqual(source.call_args.kwargs['after'], round(datetime.fromisoformat(AFTER.replace('Z', '+00:00')).timestamp() * 1_000_000))

    async def test_failure_types_keep_the_same_failure_contract_without_gap_fill(self):
        for failure in (OSError, sqlite3.OperationalError, TimeoutError, SQLiteMigrationError):
            with self.subTest(failure=failure):
                context, source, forbidden = self.context()
                source.side_effect = failure('synthetic')
                self.assertEqual(await read_ranked(context, self.arguments()),
                                 {'error': 'Ranked observations could not be read.'})
                forbidden.assert_not_awaited()

    async def test_capacity_empty_unknown_and_watched_zero_are_distinct(self):
        context, _, _ = self.context({**contracts.fixture(), 'next_offset': 10000})
        self.assertEqual((await read_ranked(context, self.arguments()))['cause'], 'capacity')
        for rows, unobserved in [([], [41]), ([account('#P0', status='never_read')], []),
                                ([account('#P0', status='watched')], [])]:
            context, _, forbidden = self.context(stored(rows, unobserved))
            result = await read_ranked(context, self.arguments(direction='attacks', metric='average_stars'))
            self.assertEqual(result['history_coverage']['status'], 'empty')
            self.assertFalse(result['source_complete'])
            self.assertIsNone(result['aggregate']['average_stars']['value'])
            self.assertEqual(result['unobserved_selections'], unobserved)
            if rows:
                self.assertEqual(result['rows'][0]['attacks_used'], None if rows[0]['log_coverage']['status'] == 'never_read' else 0)
            forbidden.assert_not_awaited()

    async def test_invalid_measures_and_windows_are_refused_before_reading(self):
        for extra in ({'metric': 'average_stars'}, {'direction': 'anything'}, {'mode': 'home'},
                      {'metric': 'attackWins', 'direction': 'attacks'}, {'detail': 'raw'},
                      {'after': BEFORE}, {'member_ids': [True]}, {'mode': []}, {'include_battles': 'yes'}):
            context, source, _ = self.context()
            self.assertIn('error', await read_ranked(context, self.arguments(**extra)))
            source.assert_not_awaited()
