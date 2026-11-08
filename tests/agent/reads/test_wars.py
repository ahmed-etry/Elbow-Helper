"""Synthetic war ranking, missing captures and current-war boundaries."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from dataclasses import replace
from elbow_helper.domain.war_attendance import aggregate_attendance
from elbow_helper.agent.reads import record_wars, war_tools as war_reads
from elbow_helper.configuration.clans import CLANS
from tests.agent.features.cwl.cwl_helpers import ResultQueries
from tests.agent.engine.test_agent_plan_flow import _context


def recorded(context=None):
    """A context whose bot has the family record's war reads."""
    return replace(context or _context(), bot=SimpleNamespace(war_reads=SimpleNamespace()))


def current_checks(response):
    """The record's latest current-war checks, in the shape the current-war tool reads."""
    return patch('elbow_helper.agent.reads.wars.current_wars', AsyncMock(return_value=response))


class WarFamilyTests(unittest.IsolatedAsyncioTestCase):

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


    async def test_current_war_unknown_attack_quota_stays_unknown_after_end(self):
        response = {'items':[{'tag':CLANS['BEH'].tag,'status':'ok','data':{
            'state':'warEnded','clan':{'members':[{'tag':'#P0','name':'Synthetic'}]}}}]}
        with current_checks(response):
            result = await war_reads.current_war(recorded(),{'clan_codes':['BEH']})
        self.assertIsNone(result['rows'][0]['members'][0]['unused_attacks'])
        self.assertIsNone(result['rows'][0]['members'][0]['missed'])
