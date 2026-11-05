"""Synthetic war ranking, missing captures and current-war boundaries."""
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from dataclasses import replace
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.agent.reads import record_wars, war_tools as war_reads
from elbow_helper.configuration.clans import CLANS
from agent.cwl_helpers import ResultQueries
from agent.engine.test_agent_plan_flow import _context
from agent.families.record_stub import regular_id, regular_war, regular_wars_from


def recorded(context=None):
    """A context whose bot has the family record's war reads."""
    return replace(context or _context(), bot=SimpleNamespace(war_reads=SimpleNamespace()))


def current_checks(response):
    """The record's latest current-war checks, in the shape the current-war tool reads."""
    return patch('elbow_helper.agent.reads.wars.current_wars', AsyncMock(return_value=response))


class WarFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_completed_bec_war_keeps_entire_lineup_and_missed_attacks(self):
        from elbow_helper.features.cwl.queries import CwlQueries
        from elbow_helper.agent.engine.answer_inputs import answer_evidence

        with TemporaryDirectory():
            repository = SimpleNamespace()
            lineup = {'#P0': (0, []), '#Q0': (1, [2]), '#P2': (2, [3, 1])}
            latest = regular_war('BEC', 200, lineup, opponent='#R0', start_ts=120)
            wars = [regular_war('BEC', 100, lineup, opponent='#R0', start_ts=20), latest,
                    regular_war('BEH', 400, lineup, opponent='#R0', start_ts=320)]
            context = recorded(replace(_context(), cwl_queries=CwlQueries(repository, lambda: {})))
            tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_war_attendance')
            with regular_wars_from(*wars) as regular:
                result = await tool.handler(context, {
                    'clan_codes': ['BEC'], 'war_type': 'REG', 'latest_completed': 1})
            self.assertEqual(regular.await_args.kwargs, {'clan_codes': ['BEC'], 'after_ts': None, 'before_ts': None,
                                                         'war_ids': (), 'latest_completed': 1})
            self.assertEqual(result['war_ids'], [regular_id(latest)])
            self.assertEqual({row['player_tag']: row['missed'] for row in result['rows']},
                             {'#P0': 2, '#Q0': 1, '#P2': 0})
            projected = answer_evidence({'attendance': result}, guild=context.guild)
            # The writer states fields identical on every row, or repeating another field, once.
            rendering = projected['attendance']['row_rendering']['rows']
            restored = [{**rendering['same_on_every_row'], **row} for row in projected['attendance']['rows']]
            self.assertEqual(len(restored), len(result['rows']))
            for row, original in zip(restored, result['rows']):
                for key, value in original.items():
                    if key not in rendering['omitted_when_repeated'] and not isinstance(value, dict):
                        self.assertEqual(row[key], value, key)

    async def test_rate_ranking_is_selected_before_top_n(self):
        rows = [{'player_tag': tag, 'player_name': tag, 'clan_code': 'BEH', 'war_id': tag,
                 'attacks_expected': expected, 'attacks_used': expected - missed, 'missed': missed}
                for tag, expected, missed in [('#P0', 10, 5), ('#Q0', 1, 1)]]
        with patch('elbow_helper.agent.reads.wars.selected_wars', AsyncMock(return_value={'rows': rows, 'war_ids': ['one']})):
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
        context = replace(_context(), cwl_queries=ResultQueries(performance=lambda **kwargs: snapshot))
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
        query = AsyncMock(return_value={'rows': [], 'war_ids': [], 'coverage': {}})
        league = Mock()
        context = recorded(replace(_context(), cwl_queries=SimpleNamespace(attendance=league)))
        with patch.object(record_wars, 'regular_wars', query):
            await war_reads.selected_wars(context, {'season': '2026-09', 'clan_codes': ['BEH']}, 'REG')
            self.assertEqual(query.await_args.kwargs['after_ts'], 1788220800)
            self.assertEqual(query.await_args.kwargs['before_ts'], 1790812800)
            self.assertNotIn('season', query.await_args.kwargs)
            query.reset_mock()
            result = await war_reads.selected_wars(context, {'season': '2026-09', 'war_ids': ['one']}, 'REG')
            self.assertEqual(result['cause'], 'invalid_arguments')
            query.assert_not_awaited()
        league.assert_not_called()

    async def test_each_war_kind_needs_its_own_stored_source(self):
        league = Mock(return_value={'rows': [], 'war_ids': [], 'coverage': {}})
        window = {'clan_codes': ['BEH'], 'after': '2026-09-01T00:00:00+00:00', 'before': '2026-10-01T00:00:00+00:00'}
        without_record = replace(_context(), cwl_queries=SimpleNamespace(attendance=league))
        without_league = recorded(replace(_context(), cwl_queries=None))
        for context, kind in ((without_record, 'REG'), (without_record, 'both'), (without_league, 'CWL'),
                              (without_league, 'both')):
            with self.subTest(kind=kind, record=context is without_league):
                result = await war_reads.selected_wars(context, window, kind)
                self.assertEqual(result['cause'], 'missing_setup')
        league.assert_not_called()
        # League history alone still answers league wars.
        self.assertNotIn('error', await war_reads.selected_wars(without_record, window, 'CWL'))
        self.assertEqual((await war_reads.current_war(_context(), {'clan_codes': ['BEH']}))['cause'], 'missing_setup')

    async def test_recent_seasons_are_refused_for_regular_wars_before_any_read(self):
        regular, league = AsyncMock(), Mock()
        context = recorded(replace(_context(), cwl_queries=SimpleNamespace(attendance=league)))
        with patch.object(record_wars, 'regular_wars', regular):
            for kind in ('REG', 'both'):
                result = await war_reads.selected_wars(context, {'history_limit': 1}, kind)
                self.assertEqual((result['cause'], result['error']), ('invalid_arguments', 'Invalid CWL history limit'))
        regular.assert_not_awaited()
        league.assert_not_called()

    async def test_current_war_joins_clan_tags_instead_of_response_position(self):
        response = {'items': [{'tag': CLANS[code].tag, 'status': 'ok',
                              'data': {'state': state, 'clan': {}}}
                             for code, state in [('BE4', 'preparation'), ('BEH', 'inWar')]]}
        with current_checks(response):
            result = await war_reads.current_war(recorded(), {'clan_codes': ['BEH', 'BE4']})
        self.assertEqual([row['state'] for row in result['rows']], ['inWar', 'preparation'])

    def test_two_accounts_of_one_member_add_up_into_one_member_row(self):
        rows = aggregate_attendance([{'player_tag': tag, 'player_name': tag, 'war_id': 'one',
                                      'attacks_expected': 2, 'attacks_used': used, 'missed': 2 - used}
                                     for tag, used in [('#P0', 1), ('#Q0', 0)]], {'#P0': 41, '#Q0': 41}, 'member')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['missed'], rows[0]['attacks_expected'], rows[0]['miss_rate']), (3, 4, .75))

    def test_unconfirmed_final_capture_is_not_a_zero_miss(self):
        rows = aggregate_attendance([{'player_tag':'#P0','player_name':'Synthetic','war_id':'one',
            'missed':None,'attacks_used':0,'attacks_expected':None}], {}, 'account')
        self.assertIsNone(rows[0]['miss_rate'])
        self.assertEqual(rows[0]['unconfirmed_opportunities'], 1)

    async def test_current_unused_attacks_become_misses_only_when_war_ends(self):
        for state, missed in [('inWar', None), ('warEnded', 2)]:
            response = {'items':[{'tag':CLANS['BEH'].tag, 'status':'ok', 'data':{
                'state':state,'attacksPerMember':2,'clan':{'members':[{'tag':'#P0','name':'Synthetic'}]}}}]}
            with current_checks(response):
                result = await war_reads.current_war(recorded(), {'clan_codes':['BEH']})
            member = result['rows'][0]['members'][0]
            self.assertEqual(member['unused_attacks'], 2)
            self.assertEqual(member['missed'], missed)

    async def test_ass_keeps_all_ranks_before_account_filtering(self):
        from dataclasses import make_dataclass
        Row = make_dataclass('SyntheticScore', [('player_tag',str),('rank',int),('ass_score',float)])
        Snapshot = make_dataclass('SyntheticSnapshot', [('rows',tuple)])
        query = ResultQueries(ass_wars=lambda **selection: Snapshot(tuple(
            Row('#P'+str(index),index+1,float(30-index)) for index in range(30))))
        context = replace(_context(), cwl_queries=query)
        with patch('elbow_helper.agent.reads.wars.selected_wars',AsyncMock(return_value={'war_ids':['war']})):
            result = await war_reads.cwl_results(context, {'metric':'ass','clan_codes':['BEH'],'war_ids':['war'],
                                                          'account_tags':['#P29']})
        self.assertEqual(result['ranked_population'], 30)
        self.assertEqual(result['rows'][0]['rank'], 30)

    async def test_current_war_unknown_attack_quota_stays_unknown_after_end(self):
        response = {'items':[{'tag':CLANS['BEH'].tag,'status':'ok','data':{
            'state':'warEnded','clan':{'members':[{'tag':'#P0','name':'Synthetic'}]}}}]}
        with current_checks(response):
            result = await war_reads.current_war(recorded(),{'clan_codes':['BEH']})
        self.assertIsNone(result['rows'][0]['members'][0]['unused_attacks'])
        self.assertIsNone(result['rows'][0]['members'][0]['missed'])
