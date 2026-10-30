"""Name selection headers leave room for the requested account facts."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.plan.results import model_result, model_view
from agent.engine.test_agent_plan_flow import _context
from agent.families.test_family_paging_regressions import decode_rows


def named_accounts(names=9):
    rows, members, resolutions = [], [], []
    for index in range(names):
        member_id, name = index + 100, f'Synthetic Navigator {index}'
        accounts = [{'player_tag': f'#P{index}{alt}', 'player_name': f'{name} account {alt}',
                     'member_id': member_id, 'member_name': name}
                    for alt in range(2 if index % 9 < 5 else 1)]
        candidates = [{**account, 'names': [account['player_name'], name],
                       'sources': ['bot_account_links', 'stored_account_directory', 'current_family_roster'],
                       'membership_status': 'present'} for account in accounts]
        resolutions.append({'query': name, 'status': 'resolved', 'look_in': 'game',
            'found_in': 'game', 'candidates': candidates, 'matching_members': [{
                'member_id': member_id, 'member_name': name, 'username': f'synthetic_{index}',
                'nickname': name,
                'accounts': [{'player_tag': a['player_tag'], 'player_name': a['player_name']}
                             for a in accounts], 'account_links_status': 'available'}],
            'selected_accounts': accounts})
        members.append({'member_id': member_id, 'member_name': name, 'nickname': name,
            'joined_at': '2026-01-01T00:00:00+00:00', 'membership_status': 'present',
            'account_tags': [a['player_tag'] for a in accounts], 'account_links_status': 'available',
            'home_clans': [{'clan_code': 'SYN'}], 'home_clan_status': 'resolved', 'roles': []})
        rows.extend({**account, 'townHallLevel': 16, 'role': 'member',
            'ranked_league': {'id': 42, 'name': 'Synthetic league'}, 'legacy_league': None,
            'clan': {'tag': '#SYN', 'name': 'Synthetic clan'}, 'trophies': 1000,
            'donations': 23, 'war_stars': 123, 'observed_at': '2026-01-01T00:00:00+00:00',
            'profile_status': 'ok'} for account in accounts)
    return {'family': 'read_member_accounts', 'rows': rows, 'members': members,
            'name_resolutions': resolutions, 'selection_complete': True}


def view_size(payload):
    return len(json.dumps(model_view(model_result(payload)), ensure_ascii=False, separators=(',', ':')))


class NameResolutionViewTests(unittest.IsolatedAsyncioTestCase):
    def test_resolved_lookups_keep_only_selection_and_fallback_source(self):
        payload = named_accounts()
        before = deepcopy(payload)
        for family in ('read_member_accounts', 'read_ranked_activity', 'read_test_family'):
            view = family_model_view({**payload, 'family': family, 'rows': []})
            for raw, shown in zip(payload['name_resolutions'], view['name_resolutions']):
                self.assertEqual(set(shown), {'query', 'status', 'look_in', 'found_in', 'selected_accounts'})
                self.assertEqual(shown['selected_accounts'], [
                    {'player_tag': a['player_tag']} for a in raw['selected_accounts']])
            self.assertEqual(family_model_view(view), view)
        self.assertEqual(payload, before)

    def test_ambiguous_source_keeps_choices_and_linked_names(self):
        for side in ('game', 'discord'):
            payload = named_accounts(1)
            payload['name_resolutions'][0].update(status='ambiguous', look_in=side, found_in=side,
                                                  selected_accounts=[])
            raw = deepcopy(payload)
            view = family_model_view(payload)
            resolution = view['name_resolutions'][0]
            self.assertEqual(resolution['status'], 'ambiguous')
            if side == 'game':
                self.assertEqual(resolution['candidates'], [{key: a[key] for key in
                    ('player_tag', 'player_name', 'member_id', 'member_name')}
                    for a in raw['name_resolutions'][0]['candidates']])
                self.assertNotIn('matching_members', resolution)
            else:
                self.assertEqual(resolution['matching_members'], [{
                    'member_id': 100, 'member_name': 'Synthetic Navigator 0',
                    'account_links_status': 'available', 'accounts': [
                        {'player_tag': '#P00', 'player_name': 'Synthetic Navigator 0 account 0'},
                        {'player_tag': '#P01', 'player_name': 'Synthetic Navigator 0 account 1'}]}])
                self.assertNotIn('candidates', resolution)
            self.assertEqual(family_model_view(view), view)
            self.assertEqual(payload, raw)

    async def test_nine_names_fourteen_accounts_fit_one_page_and_report_is_lossless(self):
        payload = named_accounts()
        context = _context()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = await tool.handler(context, {})
        self.assertEqual(len(decode_rows(model_view(model_result(page))['rows'])), 14)
        self.assertIsNone(page['next_cursor'])
        self.assertLessEqual(view_size(page), 16_000)
        with patch('elbow_helper.agent.families.model_view.name_resolution_view', side_effect=lambda r: r):
            before_size = view_size(page)
        self.assertLess(view_size(page), before_size)
        stored = json.loads(context.state.reports[page['report_id']].payload)
        self.assertEqual(stored['summary']['name_resolutions'], payload['name_resolutions'])
        self.assertEqual(stored['rows'], payload['rows'])

    async def test_larger_selection_pages_rows_without_mutating_retained_names(self):
        payload = named_accounts(40)
        context = _context()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = await tool.handler(context, {})
        self.assertNotIn('error', page)
        self.assertTrue(page['rows'])
        self.assertIsNotNone(page['next_cursor'])
        self.assertLessEqual(view_size(page), 16_000)
        self.assertEqual(len(json.loads(context.state.reports[page['report_id']].payload)['rows']),
                         len(payload['rows']))

    async def test_indivisible_large_header_reports_capacity_and_retains_full_report(self):
        payload = named_accounts(200)
        context = _context()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = await tool.handler(context, {})
        self.assertEqual(page['cause'], 'capacity')
        retained = json.loads(context.state.reports[page['report_id']].payload)
        self.assertEqual(retained['summary']['name_resolutions'], payload['name_resolutions'])
        self.assertEqual(retained['rows'], payload['rows'])


if __name__ == '__main__':
    unittest.main()
