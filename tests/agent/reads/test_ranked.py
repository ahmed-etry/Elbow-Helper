"""Ranked activity reads stored battles from the family record and keeps unknown history unknown."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import ranked as reads
from tests.agent.engine.test_agent_plan_flow import _context


AFTER, BEFORE = '2026-10-01T00:00:00Z', '2026-10-03T00:00:00Z'
SECOND = 1_000_000
DAY_TWO = 1790899200 * SECOND  # 2026-10-02T00:00:00Z
BATTLE = {'battleTimestamp':'20261002T000000.000Z','battleType':'ranked','attack':True,
          'opponentPlayerTag':'#P2','stars':3,'destructionPercentage':100}


def coverage(status='partial', gaps=()):
    return {'status': status, 'watched_from': DAY_TWO - 3600 * SECOND if status != 'never_read' else None,
            'last_read_at': DAY_TWO if status != 'never_read' else None, 'gaps': list(gaps),
            'failed_reads_in_window': 0, 'skipped_entries': 0, 'reads': 2, 'watchers': ['record'],
            'continuous_state_known': False}


def account(tag, *, battles=(), rank=None, status='partial', attacks=None):
    battles = [{'battle_at': DAY_TWO, 'clan_tag': None, 'clan_basis': 'unknown', 'payload': battle}
               for battle in battles]
    known = bool(battles) or status != 'never_read'
    return {'player_tag': tag, 'member_id': 41, 'player_name': None, 'clan_tag': None, 'status': 'unavailable',
            'selection_rank': rank, 'ranking': {}, 'ranking_observed_at': None,
            'attacks_used': (sum(b['payload']['attack'] for b in battles) if attacks is None else attacks) if known else None,
            'defenses': sum(not b['payload']['attack'] for b in battles) if known else None, 'daily': [],
            'battle_records': len(battles), 'old_store_battles': 0, 'counts_complete': False,
            'log_coverage': coverage(status), 'selected_by': [tag], 'battles': battles}


def stored(accounts, unobserved=()):
    return {'after': 0, 'before': 1, 'as_of': 1, 'selector': 'member_ids', 'accounts': list(accounts),
            'total_accounts': len(accounts), 'offset': 0, 'next_offset': None,
            'unobserved_selections': list(unobserved), 'coverage_complete': False, 'bases': {}}


class RankedFamilyTests(unittest.IsolatedAsyncioTestCase):

    def context(self, result):
        window = AsyncMock(return_value=result)
        return replace(_context(), bot=SimpleNamespace(ranked_reads=SimpleNamespace(window=window))), window

    async def test_seasonal_clan_activity_uses_community_accounts_not_parked_members(self):
        roster = {'rows': [{'player_tag': '#Q0'}], 'clans': [{'clan_code': 'BE4', 'basis': 'league_group_cohort'}]}
        context, window = self.context(stored([account('#Q0')]))
        with patch('elbow_helper.agent.reads.identity_reads.clan_rosters', AsyncMock(return_value=roster)) as source:
            result = await reads.read_ranked(context, {'clan_tags': ['BE4'], 'after': AFTER, 'before': BEFORE})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#Q0'])
        self.assertEqual(source.call_args.args[1]['basis'], 'community')
        self.assertEqual(window.call_args.args, ('account_tags', ['#Q0']))
        self.assertEqual(result['membership_coverage'], roster['clans'])


    async def test_stored_battles_give_metrics_and_an_unread_log_stays_unknown(self):
        defense = dict(BATTLE, attack=False, stars=1, destructionPercentage=40)
        context, _ = self.context(stored([account('#P0', battles=[BATTLE, defense]),
                                          account('#P1', status='never_read')]))
        result = await reads.read_ranked(context, {'member_ids':[41],'after':AFTER,'before':BEFORE,
                                                       'include_battles': True})
        rows = {row['player_tag']: row for row in result['rows']}
        self.assertEqual(rows['#P0']['observed_attacks']['battles'], 1)
        self.assertEqual(rows['#P0']['observed_attacks']['average_stars']['numerator'], 3)
        self.assertEqual(rows['#P0']['observed_defenses']['stars_total'], 1)
        self.assertEqual(rows['#P0']['watch_status'], 'partial')
        self.assertEqual(rows['#P0']['battles'][0]['battle_at'], '2026-10-02T00:00:00+00:00')
        self.assertFalse(rows['#P0']['historical_counts_complete'])
        self.assertIsNone(rows['#P1']['observed_attacks'])
        self.assertIsNone(rows['#P1']['attacks_used'])
        self.assertEqual(rows['#P1']['watch_status'], 'never_read')
        self.assertNotIn('live_coverage', result)


    async def test_a_selection_over_capacity_is_refused(self):
        context, _ = self.context({**stored([account('#P0')]), 'next_offset': 10000})
        result = await reads.read_ranked(context, {'member_ids':[41],'after':AFTER,'before':BEFORE})
        self.assertEqual(result['cause'], 'capacity')

    async def test_missing_account_selector_returns_one_specific_input_gap(self):
        context, _ = self.context(stored([]))
        result = await reads.read_ranked(context,{'after':AFTER,'before':BEFORE})
        self.assertIn('Choose members, accounts or clans',result['error'])
