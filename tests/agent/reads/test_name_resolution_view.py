"""Name selection headers leave room for the requested account facts."""
import json
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.reads.snapshots import family_tool
from tests.agent.request_fixtures import flow_context as _context


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


class NameResolutionViewTests(unittest.IsolatedAsyncioTestCase):


    async def test_larger_selection_pages_rows_without_mutating_retained_names(self):
        payload = named_accounts(40)
        context = _context()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = await tool.handler(context, {'include_profile': True})
        self.assertNotIn('error', page)
        self.assertTrue(page['rows'])
        self.assertIsNone(page['next_cursor'])
        self.assertEqual(page['rows'], payload['rows'])
        self.assertEqual(len(json.loads(context.state.reports[page['report_id']].payload)['rows']),
                         len(payload['rows']))

    async def test_indivisible_large_header_keeps_a_complete_successful_report(self):
        payload = named_accounts(200)
        context = _context()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = await tool.handler(context, {})
        self.assertNotIn('error', page)
        self.assertEqual((page['returned_rows'], page['total_rows'], page['next_offset']),
                         (len(payload['rows']), len(payload['rows']), None))
        retained = json.loads(context.state.reports[page['report_id']].payload)
        self.assertEqual(retained['summary']['name_resolutions'], payload['name_resolutions'])
        self.assertEqual(retained['rows'], payload['rows'])


if __name__ == '__main__':
    unittest.main()
