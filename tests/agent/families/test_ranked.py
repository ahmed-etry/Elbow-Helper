"""Ranked activity reads stored battles from the family record and keeps unknown history unknown."""
from dataclasses import replace
from types import SimpleNamespace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import ranked as reads
from agent.engine.test_agent_plan_flow import _context


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
    def test_default_catalogue_contains_families_help_and_explicit_discovery(self):
        from elbow_helper.agent.engine.service import AgentService
        from agent.engine.test_agent_plan_flow import _Model, _Session
        flow, _ = AgentService(_Model(_Session([], [])))._start_flow(question='Synthetic request',
            local_context='', context=_context(), conversation_history='')
        self.assertEqual(set(flow.registry), {'open_capability_group', 'read_war_attendance',
            'read_discord_access', 'find_discord_roles', 'read_discord_role_change_preview',
            'read_cwl_results', 'read_member_accounts', 'read_clan_rosters', 'read_member_cohort',
            'read_recruitment_pipeline', 'read_hibernation', 'read_current_war', 'read_promotion_cases',
            'read_community_reference', 'read_ranked_activity', 'read_conversation_evidence', 'read_bot_command_help'})

    def context(self, result):
        window = AsyncMock(return_value=result)
        return replace(_context(), bot=SimpleNamespace(ranked_reads=SimpleNamespace(window=window))), window

    async def test_seasonal_clan_activity_uses_community_accounts_not_parked_members(self):
        roster = {'rows': [{'player_tag': '#Q0'}], 'clans': [{'clan_code': 'BE4', 'basis': 'league_group_cohort'}]}
        context, window = self.context(stored([account('#Q0')]))
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)) as source:
            result = await reads.read_ranked(context, {'clan_tags': ['BE4'], 'after': AFTER, 'before': BEFORE})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#Q0'])
        self.assertEqual(source.call_args.args[1]['basis'], 'community')
        self.assertEqual(window.call_args.args, ('account_tags', ['#Q0']))
        self.assertEqual(result['membership_coverage'], roster['clans'])

    async def test_family_limit_never_renders_raw_fields_or_a_plan_failure_line(self):
        from elbow_helper.agent.engine.service import AgentService
        from elbow_helper.agent.engine.flow import AnswerFlow
        from elbow_helper.agent.engine.rounds import AgentLimitReached
        from agent.engine.test_agent_plan_flow import _Model, _Session
        context = _context()
        async def stopped(flow):
            flow.context.state.grounding_evidence = {'profiles': {'rows': [{'name': 'Synthetic', 'count': 4}]}}
            raise AgentLimitReached('Synthetic exhausted plan')
        with patch.object(AnswerFlow, 'run', stopped):
            answer = await AgentService(_Model(_Session([], []))).answer(question='Synthetic fact',
                local_context='', context=context)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn('Synthetic exhausted plan', answer)
        self.assertEqual(context.state.terminal_cause, 'limit')

    async def test_full_selection_exceeds_old_twenty_five_account_page(self):
        accounts = [account(f'#P{index}', rank=index + 1, status='never_read') for index in range(31)]
        context, window = self.context(stored(accounts))
        result = await reads.read_ranked(context, {'member_ids':[41],'after':AFTER,'before':BEFORE})
        self.assertEqual(len(result['rows']),31)
        self.assertEqual(result['rows'][-1]['selection_rank'],31)
        self.assertIsNone(result['rows'][-1]['observed_attacks'])
        self.assertEqual(window.call_args.kwargs['limit'], 10000)
        self.assertTrue(window.call_args.kwargs['include_battles'])

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

    async def test_no_live_clan_roster_or_battle_log_is_read(self):
        context, window = self.context(stored([account('#P0', battles=[BATTLE])]))
        with patch('elbow_helper.agent.datasets.clash.read_clash', AsyncMock(side_effect=AssertionError('live'))):
            result = await reads.read_ranked(context, {'clan_tags': ['#Q0'], 'after': AFTER, 'before': BEFORE,
                                                           'membership_basis': 'current_in_game'})
        self.assertEqual(window.call_args.args, ('clan_tags', ['#Q0']))
        self.assertEqual(result['rows'][0]['observed_attacks']['battles'], 1)

    async def test_a_selection_over_capacity_is_refused(self):
        context, _ = self.context({**stored([account('#P0')]), 'next_offset': 10000})
        result = await reads.read_ranked(context, {'member_ids':[41],'after':AFTER,'before':BEFORE})
        self.assertEqual(result['cause'], 'capacity')

    async def test_missing_account_selector_returns_one_specific_input_gap(self):
        context, _ = self.context(stored([]))
        result = await reads.read_ranked(context,{'after':AFTER,'before':BEFORE})
        self.assertIn('Choose members, accounts or clans',result['error'])
