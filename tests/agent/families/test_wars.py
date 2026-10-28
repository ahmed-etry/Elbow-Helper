"""Synthetic war identity, ranking, missing captures and current-war boundaries."""
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from dataclasses import replace
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.agent.families import war_reads
from elbow_helper.configuration.clans import CLANS
from agent.engine.test_agent_plan_flow import _context


class WarFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_completed_bec_war_keeps_entire_lineup_and_missed_attacks(self):
        from elbow_helper.features.cwl.queries import CwlQueries
        from elbow_helper.agent.engine.answer_inputs import answer_evidence

        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for identity, kind, code, state, end in (
                    ('old', 'REG', 'BEC', 'warEnded', 100),
                    ('latest', 'REG', 'BEC', 'warEnded', 200),
                    ('ongoing', 'REG', 'BEC', 'inWar', 300),
                    ('other_clan', 'REG', 'BEH', 'warEnded', 400),
                    ('league', 'CWL', 'BEC', 'warEnded', 500),
                ):
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                        opponent_tag,state,preparation_start_ts,start_ts,end_ts,last_seen_ts,
                        team_size,attacks_per_member) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, kind, code, CLANS[code].tag, '#R0', state,
                         end - 90, end - 80, end, end, 3, 2))
                    for tag, name, used in (('#P0', 'Zero', 0), ('#Q0', 'One', 1), ('#P2', 'Both', 2)):
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,
                            player_tag,player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''', (identity, code, tag, name, 2, used, state, end))
            context = replace(_context(), cwl_queries=CwlQueries(repository, lambda: {}))
            tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_war_attendance')
            result = await tool.handler(context, {
                'clan_codes': ['BEC'], 'war_type': 'REG', 'latest_completed': 1})
            self.assertEqual(result['war_ids'], ['latest'])
            self.assertEqual({row['player_tag']: row['missed'] for row in result['rows']},
                             {'#P0': 2, '#Q0': 1, '#P2': 0})
            projected = answer_evidence({'attendance': result}, guild=context.guild)
            self.assertEqual(projected['attendance']['rows'], result['rows'])

    async def test_rate_ranking_is_selected_before_top_n(self):
        rows = [{'player_tag': tag, 'player_name': tag, 'clan_code': 'BEH', 'war_id': tag,
                 'attacks_expected': expected, 'attacks_used': expected - missed, 'missed': missed}
                for tag, expected, missed in [('#P0', 10, 5), ('#Q0', 1, 1)]]
        with patch.object(war_reads, 'selected_wars', AsyncMock(return_value={'rows': rows, 'war_ids': ['one']})):
            result = await war_reads.attendance(_context(), {'war_type': 'REG', 'sort': 'miss_rate', 'top_n': 1})
        self.assertEqual(result['rows'][0]['player_tag'], '#Q0')
        self.assertEqual(result['ranked_population'], 2)
        self.assertIn('miss_rate', result['ordering'])

    async def test_mega_ass_top_n_preserves_multi_season_rank(self):
        from dataclasses import make_dataclass
        Row = make_dataclass('SyntheticMegaRow', [('player_tag', str), ('clan_code', str),
            ('season', str), ('rank', int), ('multi_season_rank', int)])
        Snapshot = make_dataclass('SyntheticMegaSnapshot', [('rows', tuple), ('seasons', tuple)])
        snapshot = Snapshot((Row('#P0', 'BEH', '2026-09', 1, 2), Row('#Q0', 'BEH', '2026-09', 2, 1)), ('2026-09', '2026-08'))
        context = replace(_context(), cwl_queries=SimpleNamespace(performance=lambda **kwargs: snapshot))
        result = await war_reads.cwl_results(context, {'metric': 'mega_ass', 'clan_codes': ['BEH'], 'top_n': 1})
        self.assertEqual(result['rows'][0]['player_tag'], '#Q0')
        self.assertEqual(result['rows'][0]['multi_season_rank'], 1)
        self.assertEqual(result['ranked_population'], 2)

    async def test_current_league_does_not_invent_historical_participation(self):
        source = {'items': [{'tag': CLANS['BEH'].tag, 'status': 'ok', 'data': {'warLeague': {'name': 'Synthetic league'}}}]}
        with patch.object(war_reads, 'read_clash', AsyncMock(return_value=source)):
            result = await war_reads.cwl_results(_context(), {'metric': 'league', 'clan_codes': ['BEH'], 'season': '2026-09'})
        self.assertEqual(result['current_clan_leagues'][0]['current_league']['name'], 'Synthetic league')
        self.assertIsNone(result['current_clan_leagues'][0]['season_league'])
        self.assertEqual(result['rows'], [])
        self.assertTrue(result['missing_sources'])

    async def test_regular_season_resolves_to_utc_dates_without_a_cwl_season_filter(self):
        query = Mock(return_value={'rows': [], 'war_ids': [], 'coverage': {}})
        context = replace(_context(), cwl_queries=SimpleNamespace(attendance=query))
        await war_reads.selected_wars(context, {'season': '2026-09', 'clan_codes': ['BEH']}, 'REG')
        self.assertEqual(query.call_args.kwargs['after_ts'], 1788220800)
        self.assertEqual(query.call_args.kwargs['before_ts'], 1790812800)
        self.assertIsNone(query.call_args.kwargs['season'])
        query.reset_mock()
        result = await war_reads.selected_wars(context, {'season': '2026-09', 'war_ids': ['one']}, 'REG')
        self.assertEqual(result['cause'], 'invalid_arguments')
        query.assert_not_called()

    def test_incompatible_quotas_and_missing_identity_do_not_collapse_wars(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for identity, quota, start in [('first', 1, 20), ('second', 2, 20), ('unknown', 2, 0)]:
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,
                        state,preparation_start_ts,start_ts,end_ts,last_seen_ts,team_size,attacks_per_member)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, 'REG', 'BEH', '#P0', '#Q0', 'warEnded', 10, start, 100, 1, 2, quota))
            result = repository.attendance(clan_codes=['BEH'], war_type='REG')
        self.assertEqual(len(result['war_ids']), 3)
        self.assertEqual(result['duplicate_identities_removed'], 0)
        self.assertEqual(result['unproven_war_ids'], ['unknown'])
        self.assertFalse(result['coverage']['exact_ranking'])

    def test_conflicting_final_rosters_keep_unknown_misses(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for identity, expected in [('old', 1), ('new', 2)]:
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,
                        state,preparation_start_ts,start_ts,end_ts,last_seen_ts,team_size,attacks_per_member)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, 'REG', 'BEH', '#P0', '#Q0', 'warEnded', 10, 20, 100, 1, 1, 2))
                    connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,
                        player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                        VALUES(?,?,?,?,?,?,?,?)''', (identity, 'BEH', '#P0', 'Synthetic', expected, 0, 'warEnded', 1))
            result = repository.attendance(clan_codes=['BEH'], war_type='REG')
        self.assertIsNone(result['rows'][0]['missed'])
        self.assertIn('conflicting_final_rosters', result['identity_conflicts'][0]['reasons'])

    async def test_current_war_joins_clan_tags_instead_of_response_position(self):
        response = {'items': [{'tag': CLANS[code].tag, 'status': 'ok',
                              'data': {'state': state, 'clan': {}}}
                             for code, state in [('BE4', 'preparation'), ('BEH', 'inWar')]]}
        with patch.object(war_reads, 'read_clash', AsyncMock(return_value=response)):
            result = await war_reads.current_war(_context(), {'clan_codes': ['BEH', 'BE4']})
        self.assertEqual([row['state'] for row in result['rows']], ['inWar', 'preparation'])

    def test_alternate_end_ids_are_canonical_before_window_filter_and_aggregation(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory)/'wars.db')
            repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                for identity, end, seen in [('old', 100, 1), ('new', 110, 2)]:
                    connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,
                        state,preparation_start_ts,start_ts,end_ts,last_seen_ts,team_size,attacks_per_member) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity,'REG','BEH','#P0','#Q0','warEnded',10,20,end,seen,2,2))
                    for tag, expected, used in [('#P0',2,1),('#Q0',2,0)]:
                        connection.execute('''INSERT INTO war_roster_members(war_id,clan_code,player_tag,
                            player_name,attacks_expected,attacks_used,roster_state,captured_ts)
                            VALUES(?,?,?,?,?,?,?,?)''', (identity,'BEH',tag,tag,expected,used,'warEnded',seen))
            snapshot = repository.attendance(clan_codes=['BEH'],war_type='REG',after_ts=105,before_ts=120)
            self.assertEqual(snapshot['war_ids'], ['new'])
            self.assertEqual(snapshot['duplicate_identities_removed'], 1)
            rows = aggregate_attendance(snapshot['rows'], {'#P0':41,'#Q0':41}, 'member')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['missed'], 3)
            self.assertEqual(rows[0]['attacks_expected'], 4)
            self.assertEqual(rows[0]['miss_rate'], .75)
            self.assertEqual(repository.attendance(clan_codes=['BEH'],war_type='REG',before_ts=105)['rows'], [])

    def test_unconfirmed_final_capture_is_not_a_zero_miss(self):
        rows = aggregate_attendance([{'player_tag':'#P0','player_name':'Synthetic','war_id':'one',
            'missed':None,'attacks_used':0,'attacks_expected':None}], {}, 'account')
        self.assertIsNone(rows[0]['miss_rate'])
        self.assertEqual(rows[0]['unconfirmed_opportunities'], 1)

    async def test_current_unused_attacks_become_misses_only_when_war_ends(self):
        for state, missed in [('inWar', None), ('warEnded', 2)]:
            response = {'items':[{'tag':CLANS['BEH'].tag, 'status':'ok', 'data':{
                'state':state,'attacksPerMember':2,'clan':{'members':[{'tag':'#P0','name':'Synthetic'}]}}}]}
            with patch.object(war_reads,'read_clash',AsyncMock(return_value=response)):
                result = await war_reads.current_war(_context(), {'clan_codes':['BEH']})
            member = result['rows'][0]['members'][0]
            self.assertEqual(member['unused_attacks'], 2)
            self.assertEqual(member['missed'], missed)

    async def test_ass_keeps_all_ranks_before_account_filtering(self):
        from dataclasses import make_dataclass
        Row = make_dataclass('SyntheticScore', [('player_tag',str),('rank',int),('ass_score',float)])
        Snapshot = make_dataclass('SyntheticSnapshot', [('rows',tuple)])
        query = SimpleNamespace(ass_wars=lambda **selection: Snapshot(tuple(
            Row('#P'+str(index),index+1,float(30-index)) for index in range(30))))
        context = replace(_context(), cwl_queries=query)
        with patch.object(war_reads,'selected_wars',AsyncMock(return_value={'war_ids':['war']})):
            result = await war_reads.cwl_results(context, {'metric':'ass','clan_codes':['BEH'],'war_ids':['war'],
                                                          'account_tags':['#P29']})
        self.assertEqual(result['ranked_population'], 30)
        self.assertEqual(result['rows'][0]['rank'], 30)

    async def test_current_war_unknown_attack_quota_stays_unknown_after_end(self):
        response = {'items':[{'tag':CLANS['BEH'].tag,'status':'ok','data':{
            'state':'warEnded','clan':{'members':[{'tag':'#P0','name':'Synthetic'}]}}}]}
        with patch.object(war_reads,'read_clash',AsyncMock(return_value=response)):
            result = await war_reads.current_war(_context(),{'clan_codes':['BEH']})
        self.assertIsNone(result['rows'][0]['members'][0]['unused_attacks'])
        self.assertIsNone(result['rows'][0]['members'][0]['missed'])
