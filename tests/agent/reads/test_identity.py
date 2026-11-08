"""Synthetic identity joins, clan coverage and cohort bases."""
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads import identity_reads as reads
from elbow_helper.agent.reads import account_names
from elbow_helper.agent.reads import reference as reference_reads
from elbow_helper.agent.reads.evidence import family_model_view
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from tests.agent.engine.test_agent_plan_flow import _context


def member(identifier, joined=None):
    return SimpleNamespace(id=identifier, display_name='Synthetic '+str(identifier), name='user'+str(identifier),
        nick=None, global_name=None, roles=[], joined_at=joined)


class IdentityFamilyTests(unittest.IsolatedAsyncioTestCase):

    async def test_roster_subreads_share_deadline_and_keep_current_capacity_after_seasonal_gap(self):
        import time
        context = _context()
        deadlines = []
        async def source(bounded, args):
            deadlines.append(bounded.deadline_monotonic)
            return {'items': [{'tag': CLANS['BE4'].tag, 'status': 'ok',
                              'data': {'members': 8, 'name': 'Seasonal clan', 'memberList': []}}]}
        async def seasonal(bounded, code, season):
            deadlines.append(bounded.deadline_monotonic)
            return [], season, 'season_cohort_unavailable'
        started = time.monotonic()
        with patch.object(reads, 'read_clash', source), patch.object(reads, 'seasonal_tags', seasonal):
            result = await reads.clan_rosters(context, {'clan_codes': ['BE4']})
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertGreater(deadlines[0], started)
        self.assertLessEqual(deadlines[0], time.monotonic() + reads.TOOL_TIMEOUT_SECONDS - 3)
        self.assertEqual(result['clans'][0]['in_game_members'], 8)
        self.assertEqual(result['clans'][0]['open_in_game_slots'], 42)
        self.assertIsNone(result['clans'][0]['cohort_accounts'])
        self.assertIsNone(context.deadline_monotonic)

    async def test_account_and_qualified_discord_names_share_resolution_and_owner_provenance(self):
        context = _context()
        target = member(42)
        target.display_name, target.name = 'Vega (Pilot)', 'pilot'
        context.guild.members = [target]
        context.guild.get_member = lambda identifier: target if identifier == 42 else context.member
        linked = {
            42: [{'player_tag': '#P0', 'player_name_last_seen': 'Moon Knight'},
                 {'player_tag': '#Q0', 'player_name_last_seen': 'Side Account'}]}
        all_links = {row['player_tag']: {**row, 'discord_user_id': identifier}
                     for identifier, rows in linked.items() for row in rows}
        context = replace(context, account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {value: linked.get(value, []) for value in ids},
            get_all_links=lambda: all_links,
            get_links_by_tags=lambda tags: {tag: all_links.get(tag) for tag in tags}))
        profiles = {'#P0': {'status': 'ok', 'data': {'name': 'Moon Knight'}},
                    '#Q0': {'status': 'ok', 'data': {'name': 'Side Account'}}}
        with patch.object(reads, 'profiles', AsyncMock(return_value=profiles)), \
             patch.object(account_names, 'read_clash', AsyncMock(return_value={'items': []})):
            for name in ('Moon Knight', 'Vega Pilot'):
                result = await reads.member_accounts(context, {'name': name, 'look_in': 'game'})
                self.assertEqual(result['status'], 'resolved')
                self.assertEqual({row['player_tag'] for row in result['rows']},
                                 {'#P0'} if name == 'Moon Knight' else {'#P0', '#Q0'})
                self.assertEqual(result['members'][0]['account_tags'], ['#P0', '#Q0'])
                self.assertEqual(result['member_count'], 1)
                self.assertIn(42, context.state.resolved_members)


    async def test_bew_self_role_remains_extra_participation_after_projection_and_rename(self):
        context = _context()
        role = SimpleNamespace(id=CLANS['BEW'].member_role_id, name='War Specialist', is_default=lambda: False)
        for access_role in context.member.roles:
            access_role.name = 'Access role'
            access_role.is_default = lambda: False
        context.member.roles = [*context.member.roles, role]
        context.guild.roles = [role]
        for name in ('War Specialist', 'Renamed extra wars role'):
            with self.subTest(name=name):
                role.name = name
                result = await reads.member_accounts(context, {'member_ids': [context.member.id]})
                projected = answer_evidence({'accounts': family_model_view({
                    **result, 'family': 'read_member_accounts',
                    'selection': {'include_profile': True}})}, guild=context.guild)
                row = projected['accounts']['members'][0]
                self.assertEqual(row['home_clans'], [])
                self.assertEqual(row['home_clan_status'], 'no_home_role')
                participation = row['participation_clans'][0]
                self.assertEqual((participation['clan_code'], participation['role_id'], participation['role_name']),
                                 ('BEW', role.id, name))
                reference = await reference_reads.community_reference(context,
                    {'reference_kind': 'clans', 'clan_codes': ['BEW']})
                self.assertEqual(reference['rows'][0]['roles'][0],
                    {'role_id': role.id, 'name': name, 'purpose': 'member', 'status': 'resolved'})
                self.assertFalse(reference['rows'][0]['home_placement_eligible'])
                war_role = next(row for row in reference['rows'][0]['roles'] if row['purpose'] == 'war')
                self.assertIsNone(war_role['name'])
                self.assertEqual(war_role['status'], 'unavailable')

    async def test_acceptance_window_does_not_use_discord_join_dates(self):
        context = _context()
        context.guild.members = [member(8, datetime(2020, 1, 1, tzinfo=timezone.utc)),
                                 member(9, datetime(2026, 9, 1, tzinfo=timezone.utc))]
        context.guild.get_member = lambda identifier: next((m for m in context.guild.members if m.id == identifier),
                                                         context.member if identifier == context.member.id else None)
        trials = {'rows': [{'applicant_member_id': 8, 'start_at': '2026-09-05T00:00:00+00:00'},
                             {'applicant_member_id': 9, 'start_at': '2026-08-05T00:00:00+00:00'}]}
        with patch('elbow_helper.agent.reads.workflow_reads.active_trial_rows',
                   AsyncMock(return_value=trials)):
            result = await reads.member_cohort(context, {'basis': 'accepted_recruitment',
                'accepted_after': '2026-09-01T00:00:00+00:00', 'accepted_before': '2026-10-01T00:00:00+00:00'})
        self.assertEqual([row['member_id'] for row in result['rows']], [8])
        self.assertEqual(result['rows'][0]['accepted_at'], '2026-09-05T00:00:00+00:00')
        self.assertFalse(result['historical_acceptance_complete'])
        self.assertFalse(result['count_complete'])

    async def test_missing_join_date_remains_an_unconfirmed_window_candidate(self):
        context = _context()
        context.guild.members = [member(8)]
        context.guild.get_member = lambda identifier: (context.member if identifier == context.member.id
            else next((m for m in context.guild.members if m.id == identifier), None))
        result = await reads.member_cohort(context, {'basis': 'joined_discord',
            'joined_after': '2026-09-01T00:00:00+00:00'})
        self.assertEqual(result['rows'][0]['selection_status'], 'unknown_date')
        self.assertFalse(result['count_complete'])


    async def test_all_clans_include_cwl_only_cohort_without_parked_members(self):
        response = {'items':[{'tag':clan.tag,'status':'ok','data':{'members':50,
            'memberList':[{'tag':'#P0','name':'Parked','townHallLevel':3}]}} for clan in CLANS.values()]}
        async def seasonal(context, code, season):
            return ['#Q0'], '2026-10', 'league_group_cohort'
        with patch.object(reads,'read_clash',AsyncMock(return_value=response)), \
             patch.object(reads,'seasonal_tags',seasonal), \
             patch.object(reads,'profiles',AsyncMock(return_value={'#Q0':{'status':'ok','data':{'name':'Seasonal','townHallLevel':18}}})):
            result = await reads.clan_rosters(_context(), {})
        self.assertEqual({row['clan_code'] for row in result['clans']}, set(CLANS))
        be4 = [row for row in result['rows'] if row['clan_code']=='BE4']
        self.assertEqual([row['player_name'] for row in be4], ['Seasonal'])
        self.assertEqual(be4[0]['basis'], 'league_group_cohort')
        self.assertEqual(len(result['clans']), 9)

    async def test_join_cohort_is_sorted_before_paging_with_unknown_dates_last(self):
        context = _context()
        context.guild.members = [member(2,None), member(9,datetime(2026,1,1,tzinfo=timezone.utc)),
                                 member(3,datetime(2026,1,1,tzinfo=timezone.utc))]
        context.guild.get_member = lambda identifier: (context.member if identifier == context.member.id
            else next((m for m in context.guild.members if m.id == identifier), None))
        result = await reads.member_cohort(context, {'basis':'joined_discord','sort':'oldest_first'})
        self.assertEqual([row['member_id'] for row in result['rows']], [3,9,2])
        self.assertIn('not recruitment acceptance', result['coverage'])

    async def test_colliding_exact_names_return_candidates_without_selecting_a_subject(self):
        context = _context()
        context.guild.members = [member(42),member(43)]
        for actor in context.guild.members:
            actor.display_name = 'Same name'
        context.guild.get_member=lambda identifier: context.member if identifier == context.member.id else next(
            (row for row in context.guild.members if row.id==identifier), None)
        with patch.object(account_names, 'read_clash', AsyncMock(return_value={'items': []})):
            result = await reads.member_accounts(context, {'name':'Same name', 'look_in': 'discord'})
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual([row['member_id'] for row in result['rows']], [42,43])
        self.assertTrue(all(row['player_tag'] is None for row in result['rows']))
        self.assertTrue(all(row['candidate_for'] == ['Same name'] for row in result['rows']))
        self.assertEqual(result['resolved_member_ids'], [])
        self.assertEqual(result['name_resolutions'][0]['status'], 'ambiguous')
        self.assertEqual([row['member_id'] for row in
            result['name_resolutions'][0]['matching_members']], [42,43])
        self.assertEqual([row['member_id'] for row in result['members']], [42,43])
        self.assertTrue(all(row['candidate_for'] == ['Same name'] for row in result['members']))
        self.assertEqual(context.state.resolved_members, {})

    async def test_roster_filters_precede_cohort_counts_but_not_current_capacity(self):
        response = {'items':[{'tag':CLANS['BEC'].tag,'status':'ok','data':{'members':48,
            'memberList':[{'tag':'#P0','townHallLevel':17,'role':'member'},
                          {'tag':'#Q0','townHallLevel':17,'role':'leader'},
                          {'tag':'#P2','townHallLevel':16,'role':'member'}]}}]}
        with patch.object(reads,'read_clash',AsyncMock(return_value=response)):
            result = await reads.clan_rosters(_context(),{'clan_codes':['BEC'],
                'town_hall_min':17,'town_hall_max':17,'exclude_in_game_roles':['leader','coLeader']})
        self.assertEqual(result['count'],1)
        self.assertEqual(result['clans'][0]['town_halls'],{'17':1})
        self.assertEqual(result['clans'][0]['excluded_accounts'],2)
        self.assertEqual(result['clans'][0]['in_game_capacity'], 50)
        self.assertEqual(result['clans'][0]['open_in_game_slots'], 2)

    async def test_roster_tier_filters_read_the_stored_profile_not_live_profiles(self):
        response = {'items':[{'tag':CLANS['BEC'].tag,'status':'ok','data':{'members':3,
            'memberList':[{'tag':'#P0','townHallLevel':17,'role':'member','leagueTier':{'id':1,'name':'Live'}},
                          {'tag':'#Q0','townHallLevel':17,'role':'member'},
                          {'tag':'#P2','townHallLevel':16,'role':'member'}]}}]}
        async def summary(tags, as_of=None):
            stored = {'#P0': {'id': 9, 'name': 'Legend III'}, '#Q0': {'id': 1, 'name': 'Different'}}
            return {'rows': [{'player_tag': tag, 'ranked_league': stored.get(tag),
                              'field_observed_at': {'leagueTier': 1_790_000_000_000_000} if tag in stored else {}}
                             for tag in tags]}
        people = SimpleNamespace(account_summary=AsyncMock(side_effect=summary))
        context = replace(_context(), bot=SimpleNamespace(people_reads=people))
        with patch.object(reads,'read_clash',AsyncMock(return_value=response)), \
                patch.object(reads,'profiles',AsyncMock(side_effect=AssertionError('live profiles'))):
            result = await reads.clan_rosters(context,{'clan_codes':['BEC'],'basis':'current_in_game',
                                                       'ranked_tier_ids':[9]})
        # The live roster says #P0 is in tier 1; the stored profile's tier 9 decides.
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        people.account_summary.assert_awaited_once()
        context = replace(_context(), bot=SimpleNamespace())
        with patch.object(reads,'read_clash',AsyncMock(return_value=response)):
            unknown = await reads.clan_rosters(context,{'clan_codes':['BEC'],'basis':'current_in_game',
                                                        'ranked_tier_prefix':'Legend'})
        # Without the record every tier is unknown, never the live roster's value.
        self.assertEqual(unknown['rows'], [])


    async def test_cohort_reference_opens_complete_snapshot_and_rechecks_authority(self):
        from elbow_helper.agent.reads.snapshots import family_tool
        from elbow_helper.agent.reads.projections import cohort_selection
        async def source(context,arguments):
            return {'rows':[{'member_id':index+100} for index in range(30)]}
        context = _context()
        tool = family_tool('read_member_cohort','Synthetic cohort.',{},source)
        first = await tool.handler(context,{'limit':1})
        selected = await cohort_selection(context,first['report_id'],'member_ids')
        self.assertEqual(len(selected['member_ids']),30)
        context.state.report_sources.clear()
        self.assertEqual((await cohort_selection(context,first['report_id'],'member_ids'))['cause'],'report_unavailable')

    async def test_cohort_performance_uses_one_canonical_complete_member_join(self):
        context = _context()
        context.guild.members = [member(41),member(42)]
        for role in context.member.roles:
            role.name, role.is_default = 'Core', lambda: False
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=lambda _: {},
            get_links_by_tags=lambda tags: {'#P0': {'discord_user_id': 41}}))
        source = AsyncMock(return_value={'rows':[{'player_tag':'#P0','player_name':'Synthetic','war_id':'#R0','war_type':'CWL',
            'attacks_used':0,'attacks_expected':1,'missed':1}],
                                         'coverage':'stored completed wars'})
        with patch('elbow_helper.agent.reads.wars.selected_wars',source):
            result = await reads.member_cohort(context,{'basis':'joined_discord',
                'performance':{'war_type':'CWL','season':'2026-09'}})
        self.assertEqual(source.await_count, 1)
        self.assertEqual(source.call_args.args[2], 'CWL')
        self.assertEqual(result['rows'][0]['war_attendance']['missed'],1)
        self.assertIsNone(result['rows'][1]['war_attendance'])
