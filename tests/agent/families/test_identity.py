"""Synthetic identity joins, clan coverage and cohort bases."""
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families import identity_reads as reads
from elbow_helper.agent.families import account_names
from elbow_helper.agent.families import reference_reads
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.clan_roles import ROLE_PURPOSES
from agent.engine.test_agent_plan_flow import _context


def member(identifier, joined=None):
    return SimpleNamespace(id=identifier, display_name='Synthetic '+str(identifier), name='user'+str(identifier),
        nick=None, global_name=None, roles=[], joined_at=joined)


class IdentityFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_clan_evidence_reaches_account_and_cohort_writers(self):
        target = member(42, datetime(2026, 1, 1, tzinfo=timezone.utc))
        clan = {'tag': CLANS['BEC'].tag, 'name': CLANS['BEC'].name}
        payloads = {'/players/%23P0': {'tag': '#P0', 'name': 'In clan', 'clan': clan},
                    '/players/%23Q0': {'tag': '#Q0', 'name': 'Clanless'}}

        async def get(path, **kwargs):
            payload = payloads.get(path)
            return SimpleNamespace(ok=payload is not None, status=200 if payload else 503, payload=payload)

        context = replace(_context(),
            bot=SimpleNamespace(clash_client=SimpleNamespace(configured=True, get=AsyncMock(side_effect=get))),
            account_links=SimpleNamespace(get_links_for_members=lambda ids: {
                42: [{'player_tag': tag} for tag in ('#P0', '#Q0', '#P2')]}))
        context.guild.members = [target]
        context.guild.get_member = lambda identifier: target if identifier == 42 else context.member
        for family, handler, arguments in (
            ('read_member_accounts', reads.member_accounts, {'member_ids': [42]}),
            ('read_member_cohort', reads.member_cohort, {'basis': 'joined_discord'}),
        ):
            with self.subTest(family=family):
                result = await handler(context, arguments)
                visible = answer_evidence({'accounts': family_model_view({**result, 'family': family})})['accounts']
                for evidence in (result, visible):
                    accounts = (evidence['rows'] if family == 'read_member_accounts'
                                else evidence['rows'][0]['accounts'])
                    by_tag = {row['player_tag']: row for row in accounts}
                    self.assertEqual(by_tag['#P0']['clan'], clan)
                    self.assertEqual(by_tag['#P0']['clan_status'], 'in_clan')
                    self.assertIsNone(by_tag['#Q0']['clan'])
                    self.assertEqual(by_tag['#Q0']['clan_status'], 'not_in_clan')
                    self.assertEqual(by_tag['#Q0']['profile_status'], 'ok')
                    self.assertIsNone(by_tag['#P2']['clan'])
                    self.assertNotIn('clan_status', by_tag['#P2'])
                    self.assertEqual(by_tag['#P2']['profile_status'], 'maintenance')
                unknown_count = 'unknown_profile_count' if family == 'read_member_accounts' else 'unknown_account_profiles'
                self.assertEqual(result[unknown_count], 1)
                self.assertFalse(result['count_complete'])
        self.assertNotIn('clan', payloads['/players/%23Q0'])

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

    async def test_all_configured_clan_roles_keep_live_display_names_and_purposes(self):
        context = _context()
        roles = [SimpleNamespace(id=getattr(clan, field), name=f'Display {index}-{slot}',
                                 is_default=lambda: False)
                 for index, clan in enumerate(CLANS.values())
                 for slot, field in enumerate(ROLE_PURPOSES) if getattr(clan, field) is not None]
        context.guild.roles = roles
        context.member.roles = roles
        result = reads.member_fields(context.member, context.member.id)
        by_id = {role.id: role for role in roles}
        for clan in CLANS.values():
            kind = 'participation_clans' if clan.is_utility else 'home_clans'
            binding = next(row for row in result[kind] if row['clan_code'] == clan.code)
            self.assertEqual(binding['role_name'], by_id[clan.member_role_id].name)
            if clan.is_utility:
                self.assertNotIn(clan.code, [row['clan_code'] for row in result['home_clans']])
            for field in ROLE_PURPOSES:
                role_id = getattr(clan, field)
                if role_id is None:
                    continue
                row = next(row for row in result['roles'] if row['role_id'] == role_id)
                self.assertIn({'clan_code': clan.code, 'purpose': field.removesuffix('_role_id')},
                              row['clan_purposes'])
        reference = await reference_reads.community_reference(context, {'reference_kind': 'clans'})
        projected = answer_evidence({'clans': {**reference, 'family': 'read_community_reference',
            'selection': {'reference_kind': 'clans'}}}, guild=context.guild)
        for row in projected['clans']['rows']:
            for binding in row['roles']:
                self.assertEqual(binding['name'], by_id[binding['role_id']].name)
                self.assertEqual(binding['status'], 'resolved')

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
                    **result, 'family': 'read_member_accounts'})}, guild=context.guild)
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
        with patch('elbow_helper.agent.families.workflow_reads.active_trial_rows',
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

    async def test_every_linked_account_keeps_ranked_and_legacy_leagues_distinct(self):
        actor = member(41)
        links = SimpleNamespace(get_links_for_members=lambda ids: {41:[{'player_tag':'#P0'}, {'player_tag':'#Q0'}]})
        context = replace(_context(), account_links=links)
        actor.roles = context.member.roles
        for role in actor.roles:
            role.is_default = lambda: False
            role.name = "Core"
        context.guild.get_member=lambda identifier: actor if identifier == 41 else None
        api = {'#P0':{'status':'ok','data':{'name':'One','townHallLevel':17,
               'leagueTier':{'name':'Ranked'},'league':{'name':'Legacy'}}}, '#Q0':{'status':'unavailable'}}
        with patch.object(reads, 'profiles', AsyncMock(return_value=api)):
            result = await reads.member_accounts(context, {'member_ids':[41]})
        self.assertEqual(result['account_count'], 2)
        self.assertEqual(result['rows'][0]['ranked_league']['name'], 'Ranked')
        self.assertEqual(result['rows'][0]['legacy_league']['name'], 'Legacy')
        self.assertIsNone(result['rows'][1]['townHallLevel'])
        self.assertEqual(result['rows'][1]['profile_status'], 'unavailable')

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

    async def test_cohort_joins_every_account_and_filters_actual_ranked_tier(self):
        context = _context()
        context.guild.members = [member(41),member(42),member(43)]
        accounts = {'rows':[{'member_id':41,'player_tag':'#P0','townHallLevel':17,
                            'ranked_league':{'id':9,'name':'Legend III'},'profile_status':'ok'},
                           {'member_id':41,'player_tag':'#Q0','townHallLevel':18,
                            'ranked_league':{'id':9,'name':'Legend II'},'profile_status':'ok'},
                           {'member_id':42,'player_tag':'#P2','townHallLevel':17,
                            'ranked_league':{'id':1,'name':'Different'},'profile_status':'ok'},
                           {'member_id':43,'player_tag':None,'profile_status':'unlinked'}]}
        with patch.object(reads,'member_accounts',AsyncMock(return_value=accounts)):
            result = await reads.member_cohort(context,{'basis':'joined_discord',
                                                       'include_accounts':True,'ranked_tier_ids':[9]})
        self.assertEqual(result['member_count'],1)
        self.assertEqual(result['account_count'],2)
        self.assertEqual(len(result['rows'][0]['accounts']),2)
        with patch.object(reads,'member_accounts',AsyncMock(return_value=accounts)):
            named = await reads.member_cohort(context,{'basis':'joined_discord','ranked_tier_prefix':'Legend'})
        self.assertEqual(named['member_count'],1)
        self.assertEqual(named['account_count'],2)
        accounts['rows'].append({'member_id':42,'player_tag':'#Q2',
            'ranked_league':None,'townHallLevel':None,'profile_status':'unavailable'})
        with patch.object(reads,'member_accounts',AsyncMock(return_value=accounts)):
            partial = await reads.member_cohort(context,{'basis':'joined_discord','ranked_tier_prefix':'Legend'})
        self.assertFalse(partial['count_complete'])
        self.assertEqual(partial['unknown_account_profiles'],1)

    async def test_cohort_reference_opens_complete_snapshot_and_rechecks_authority(self):
        from elbow_helper.agent.families.core import family_tool
        from elbow_helper.agent.families.projections import cohort_selection
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
        from elbow_helper.agent.families import war_reads
        source = AsyncMock(return_value={'rows':[{'player_tag':'#P0','player_name':'Synthetic','war_id':'#R0','war_type':'CWL',
            'attacks_used':0,'attacks_expected':1,'missed':1}],
                                         'coverage':'stored completed wars'})
        with patch.object(war_reads,'selected_wars',source):
            result = await reads.member_cohort(context,{'basis':'joined_discord',
                'performance':{'war_type':'CWL','season':'2026-09'}})
        self.assertEqual(source.await_count, 1)
        self.assertEqual(source.call_args.args[2], 'CWL')
        self.assertEqual(result['rows'][0]['war_attendance']['missed'],1)
        self.assertIsNone(result['rows'][1]['war_attendance'])

    async def test_tags_view_uses_all_retained_cohort_accounts_without_duplicates(self):
        from elbow_helper.agent.families.core import family_tool
        async def source(context,arguments):
            return {'rows':[{'accounts':[{'player_tag':'#P0'},{'player_tag':'#Q0'}]},
                            {'accounts':[{'player_tag':'#P0'}]}]}
        tool = family_tool('read_member_cohort','Synthetic cohort.',{},source)
        result = await tool.handler(_context(),{'limit':1,'view':'tags'})
        self.assertEqual(result['tags'],['#P0','#Q0'])
        self.assertEqual(result['rows'],[])

    async def test_two_family_reads_export_whole_cohort_in_one_plan_and_answer(self):
        import csv
        from io import StringIO
        from elbow_helper.agent.engine.service import AgentService
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
        from agent.engine.test_agent_plan_flow import _Model, _Session, _model_step, _plan
        context = _context()
        members = [member(100+index) for index in range(31)]
        context.guild.members = members
        context.guild.get_member = lambda identifier: context.member if identifier==41 else next(
            (row for row in members if row.id==identifier),None)
        alphabet = '0289PYLQGRJCUV'
        tags = ['#P'+alphabet[index//len(alphabet)]+alphabet[index%len(alphabet)] for index in range(31)]
        context = replace(context,account_links=SimpleNamespace(get_links_for_members=lambda ids:{
            row.id:[{'player_tag':tags[index]}] for index,row in enumerate(members) if row.id in ids}))
        async def profiles(context,selected):
            return {tag:{'status':'ok','data':{'name':'Synthetic','townHallLevel':17}} for tag in selected}
        plan = _plan([
            {'id':'cohort','capability':'read_member_cohort','arguments':{'basis':'joined_discord','limit':1},'depends_on':[]},
            {'id':'accounts','capability':'read_member_accounts','arguments':{
                'cohort_ref':{'step':'cohort','path':['report_id']},'delivery':'csv','limit':1},'depends_on':['cohort']},
        ])
        session = _Session([_model_step(plan),AgentStep('Found 31 accounts.',(),AgentUsage())],[])
        flow,_ = AgentService(_Model(session))._start_flow(question='Synthetic cohort export',
            local_context='',context=context,conversation_history='')
        with patch.object(reads,'profiles',profiles):
            answer = await flow.run()
        self.assertEqual(answer,'Found 31 accounts.')
        self.assertEqual(len(session.calls),2)
        exported = list(csv.reader(StringIO(context.state.attachments[0].data.decode('utf-8-sig'))))
        self.assertEqual(len(exported),32)
