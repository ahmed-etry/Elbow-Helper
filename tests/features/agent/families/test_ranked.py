"""Ranked reads finish from usable live evidence and preserve unknown history."""
from dataclasses import replace
from types import SimpleNamespace
import json
from elbow_helper.features.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.families import ranked_reads as reads
from elbow_helper.features.agent.datasets import ranked
from features.agent.engine.test_agent_plan_flow import _context


AFTER, BEFORE = '2026-10-01T00:00:00Z', '2026-10-03T00:00:00Z'
BATTLE = {'battleTimestamp':'2026-10-02T00:00:00Z','battleType':'RANKED','attack':True,
          'opponentPlayerTag':'#P2','stars':3,'destructionPercentage':100,'army':{'units':[]}}


class RankedFamilyTests(unittest.IsolatedAsyncioTestCase):
    def test_default_catalogue_contains_families_help_and_explicit_discovery(self):
        from elbow_helper.features.agent.engine.service import AgentService
        from features.agent.engine.test_agent_plan_flow import _Model, _Session
        flow, _ = AgentService(_Model(_Session([], [])))._start_flow(question='Synthetic request',
            local_context='', context=_context(), conversation_history='')
        self.assertEqual(set(flow.registry), {'open_capability_group', 'read_war_attendance',
            'read_discord_access', 'find_discord_roles', 'read_discord_role_change_preview',
            'read_cwl_results', 'read_member_accounts', 'read_clan_rosters', 'read_member_cohort',
            'read_recruitment_pipeline', 'read_hibernation', 'read_current_war', 'read_promotion_cases',
            'read_community_reference', 'read_ranked_activity', 'read_conversation_evidence', 'read_bot_command_help'})

    async def test_seasonal_clan_activity_uses_community_accounts_not_parked_members(self):
        roster = {'rows': [{'player_tag': '#Q0'}], 'clans': [{'clan_code': 'BE4', 'basis': 'league_group_cohort'}]}
        live = {'items': [{'tag': '#Q0', 'status': 'ok', 'data': {'items': []}}]}
        with patch.object(reads, 'clan_rosters', AsyncMock(return_value=roster)) as source, \
                patch.object(reads, 'read_clash', AsyncMock(side_effect=AssertionError('Parked roster read'))), \
                patch.object(ranked, '_live_history', AsyncMock(return_value=live)):
            result = await reads.ranked_activity(replace(_context(), ranked_queries=None),
                {'clan_tags': ['BE4'], 'after': AFTER, 'before': BEFORE})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#Q0'])
        self.assertEqual(source.call_args.args[1]['basis'], 'community')
        self.assertEqual(result['membership_coverage'], roster['clans'])

    async def test_family_limit_never_renders_raw_fields_or_a_plan_failure_line(self):
        from elbow_helper.features.agent.engine.service import AgentService
        from elbow_helper.features.agent.engine.flow import AnswerFlow
        from elbow_helper.features.agent.engine.rounds import AgentLimitReached
        from features.agent.engine.test_agent_plan_flow import _Model, _Session
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

    def context(self, accounts=(), battles=()):
        def window(**kwargs):
            self.assertEqual(kwargs['limit'],10000)
            self.assertTrue(kwargs['include_battles'])
            return {'accounts':list(accounts),'stored_battles':list(battles),'next_offset':None,
                    'coverage_complete':False,'complete':True}
        return replace(_context(),ranked_queries=SimpleNamespace(window=window))

    async def test_full_selection_exceeds_old_twenty_five_account_page(self):
        accounts = [{'player_tag':f'#P{index}','selection_rank':index+1,'ranking':{},
                     'attacks_used':None} for index in range(31)]
        with patch.object(ranked,'_live_history',AsyncMock(return_value={'items':[]})):
            result = await reads.ranked_activity(self.context(accounts),
                {'member_ids':[41],'after':AFTER,'before':BEFORE})
        self.assertEqual(len(result['rows']),31)
        self.assertEqual(result['rows'][-1]['selection_rank'],31)
        self.assertIsNone(result['rows'][-1]['observed_attacks'])

    async def test_empty_collection_uses_live_sample_without_claiming_complete_zero(self):
        context = replace(_context(),ranked_queries=None)
        live = {'items':[{'tag':'#P0','status':'ok','data':{'items':[BATTLE,
            dict(BATTLE,battleTimestamp='2026-09-01T00:00:00Z')]}}]}
        with patch.object(ranked,'_live_history',AsyncMock(return_value=live)) as source:
            result = await reads.ranked_activity(context,{'account_tags':['#P0'],'after':AFTER,'before':BEFORE})
        row = result['rows'][0]
        self.assertIsNone(row['attacks_used'])
        self.assertEqual(row['observed_attacks']['battles'],1)
        self.assertEqual(row['observed_attacks']['average_stars']['numerator'],3)
        self.assertEqual(row['observed_attacks']['average_stars']['denominator'],1)
        self.assertFalse(row['historical_counts_complete'])
        self.assertTrue(source.call_args.kwargs['full_detail'])

    async def test_stored_and_live_copies_are_not_summed_and_conflicts_are_visible(self):
        context = self.context([{'player_tag':'#P0','ranking':{}}],
            [{'player_tag':'#P0','payload_json':json.dumps(BATTLE)}])
        live = {'items':[{'tag':'#P0','status':'ok','data':{'items':[BATTLE]}}]}
        args = {'account_tags':['#P0'],'after':AFTER,'before':BEFORE,'include_battles':True}
        with patch.object(ranked,'_live_history',AsyncMock(return_value=live)):
            result = await reads.ranked_activity(context,args)
        row = result['rows'][0]
        self.assertEqual(row['observed_attacks']['battles'],1)
        self.assertEqual(row['duplicate_copies_removed'],1)
        self.assertEqual(row['battles'][0]['sources'],['live','stored'])
        live['items'][0]['data']['items']=[dict(BATTLE,stars=1)]
        with patch.object(ranked,'_live_history',AsyncMock(return_value=live)):
            result = await reads.ranked_activity(context,args)
        self.assertEqual(result['rows'][0]['conflicting_battles'],1)
        self.assertEqual(result['rows'][0]['observed_attacks']['battles'],0)

    async def test_missing_account_selector_returns_one_specific_input_gap(self):
        result = await reads.ranked_activity(self.context(),{'after':AFTER,'before':BEFORE})
        self.assertIn('Choose members, accounts or clans',result['error'])
