"""Leadership exclusion follows verified owners before roster presentation."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CORE, LEAD, CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID
from elbow_helper.features.agent.families import identity_reads as reads
from features.agent.engine.test_agent_plan_flow import _context


class LeadershipSelectionTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        codes = list(CLANS)[:2]
        def member(identifier, roles=()):
            return SimpleNamespace(id=identifier, display_name=f'Member {identifier}', joined_at=None,
                roles=[SimpleNamespace(id=role, name=f'Role {role}', is_default=lambda: False)
                       for role in roles])
        members = {1: member(1, (CO_APPLICANT_ROLE_ID,)),
                   2: member(2, (next(iter(CORE | LEAD)),)),
                   3: member(3, (SLEEPING_CO_ROLE_ID,)), 4: member(4),
                   7: member(7, (CO_APPLICANT_ROLE_ID,))}
        owners = {f'#P{i}': owner for i, owner in ((0, 1), (1, 1), (2, 2), (3, 3), (4, 4), (7, 7))}
        links = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {'discord_user_id': owners[tag]} for tag in tags if tag in owners})
        context = replace(_context(), account_links=links)
        context.guild.get_member = members.get
        def account(index, role='member', level=16):
            return {'tag': f'#P{index}', 'name': f'Account {index}',
                    'townHallLevel': level, 'role': role, 'donations': index}
        source = {'items': [
            {'tag': CLANS[codes[0]].tag, 'status': 'ok', 'data': {'members': 2,
                'memberList': [account(0, 'coLeader', 2), account(6, 'leader')]}},
            {'tag': CLANS[codes[1]].tag, 'status': 'ok', 'data': {'members': 6,
                'memberList': [account(i) for i in (1, 2, 3, 4, 5, 7)]}}]}
        return context, codes, source

    async def test_leader_owners_cross_clans_even_when_other_filters_hide_their_leader_account(self):
        context, codes, source = self.fixture()
        reader = AsyncMock(return_value=source)
        with patch.object(reads, 'read_clash', reader):
            result = await reads.clan_rosters(context, {'clan_codes': codes, 'basis': 'current_in_game',
                'exclude_leadership': True, 'town_hall_min': 10,
                'exclude_in_game_roles': ['leader', 'coLeader']})
        self.assertEqual({r['player_tag'] for r in result['rows']}, {'#P4', '#P5', '#P7'})
        self.assertEqual(result['count'], 3)
        self.assertFalse(result['count_complete'])
        self.assertEqual(result['leadership_filter']['unverified_accounts'], 1)
        self.assertEqual(sum(c['excluded_accounts'] for c in result['clans']), 5)
        self.assertEqual(result['clans'][1]['town_halls'], {'16': 3})
        self.assertEqual(result['clans'][1]['ownership']['unknown_owner_accounts'], 1)
        self.assertEqual([c['open_in_game_slots'] for c in result['clans']], [48, 44])
        reader.assert_awaited_once()

    def test_checked_dependent_measures_reuse_the_filtered_population(self):
        from elbow_helper.features.agent.engine.registry import build_agent_tools
        from elbow_helper.features.agent.plan.checker import check_plan
        codes = list(CLANS)[:2]
        plan = {'goal': 'Compare selected accounts', 'effort': 'high', 'output': 'text', 'steps': [
            {'id': 'cohort', 'capability': 'read_member_cohort', 'arguments': {
                'basis': 'clan_rosters', 'clan_codes': codes, 'exclude_leadership': True}, 'depends_on': []},
            {'id': 'score', 'capability': 'read_cwl_results', 'arguments': {
                'metric': 'ass', 'clan_codes': codes, 'cohort_ref': {'step': 'cohort', 'path': ['report_id']}},
             'depends_on': ['cohort']}]}
        checked = check_plan(plan, build_agent_tools())
        self.assertTrue(checked.ok, checked.error)

    async def test_known_unlinked_leaders_are_excluded_but_unknown_member_owners_remain_unverified(self):
        context, codes, source = self.fixture()
        with patch.object(reads, 'read_clash', AsyncMock(return_value=source)):
            result = await reads.clan_rosters(context, {'clan_codes': codes, 'basis': 'current_in_game',
                                                      'exclude_leadership': True})
        self.assertEqual(result['leadership_filter']['excluded_accounts'], 5)
        self.assertEqual({r['player_tag'] for r in result['rows']}, {'#P4', '#P5', '#P7'})
        unknown = next(r for r in result['rows'] if r['player_tag'] == '#P5')
        self.assertEqual(unknown['leadership']['status'], 'unknown')

    async def test_default_rosters_preserve_existing_selection(self):
        context, codes, source = self.fixture()
        with patch.object(reads, 'read_clash', AsyncMock(return_value=source)):
            result = await reads.clan_rosters(context, {'clan_codes': codes, 'basis': 'current_in_game'})
        self.assertEqual(result['count'], 8)
        self.assertTrue(result['count_complete'])
        self.assertNotIn('leadership_filter', result)

    async def test_roster_activity_cohort_uses_the_same_filtered_accounts_and_unknowns(self):
        context, codes, source = self.fixture()
        with patch.object(reads, 'read_clash', AsyncMock(return_value=source)):
            result = await reads.member_cohort(context, {'clan_codes': codes,
                'basis': 'clan_rosters', 'roster_basis': 'current_in_game', 'exclude_leadership': True})
        self.assertEqual(result['account_count'], 3)
        self.assertEqual({a['player_tag'] for r in result['rows'] for a in r['accounts']},
                         {'#P4', '#P5', '#P7'})
        self.assertEqual(result['leadership_filter']['unverified_accounts'], 1)
        self.assertFalse(result['count_complete'])
        self.assertEqual([c['open_in_game_slots'] for c in result['clans']], [48, 44])

    async def test_non_roster_basis_rejects_the_unsupported_filter_without_source_reads(self):
        context, _, _ = self.fixture()
        source = AsyncMock()
        with patch.object(reads, 'read_clash', source):
            result = await reads.member_cohort(context, {'basis': 'role_holders', 'exclude_leadership': True})
        self.assertEqual(result['cause'], 'invalid_arguments')
        source.assert_not_awaited()
