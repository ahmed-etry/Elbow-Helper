"""Frozen selection contracts for the people consolidation, with no network."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.reads.people import read_people_links as read_linked_accounts
from elbow_helper.agent.reads.people import stored_profiles, standing_rows
from elbow_helper.agent.reads import identity_reads, account_names
from tests.agent.engine.test_agent_plan_flow import _context


class PeopleContracts(unittest.IsolatedAsyncioTestCase):
    def context(self):
        self.links = {101: [{'player_tag': '#P0', 'player_name_last_seen': 'Old Pilot'},
                            {'player_tag': '#Q0', 'player_name_last_seen': 'Other Account'}],
                      102: [{'player_tag': '#R0', 'player_name_last_seen': 'Departed'}], 103: []}
        by_tag = {r['player_tag']: {**r, 'discord_user_id': i}
                  for i, rows in self.links.items() for r in rows}
        context = replace(_context(), bot=SimpleNamespace())
        context.guild.get_member = lambda identifier: context.member if identifier == context.member.id else None
        for role in context.member.roles:
            role.name, role.is_default = 'Synthetic access', lambda: False
        return replace(context, account_links=SimpleNamespace(
            get_links_for_members=Mock(side_effect=lambda ids: {i: self.links.get(i, []) for i in ids}),
            get_links_by_tags=Mock(side_effect=lambda tags: {t: by_tag.get(t) for t in tags}),
            get_all_links=Mock(return_value=by_tag)))

    async def test_links_keep_unlinked_and_departed_members_and_deduplicate_ids(self):
        context = self.context()
        context.bot.clash_client = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('Clash called')))
        context.bot.agent_model = SimpleNamespace(start_session=Mock(side_effect=AssertionError('LLM called')))
        result = await read_linked_accounts(context, {'member_ids': [102, 103, 101, 102]})
        self.assertEqual(result['member_ids'], [102, 103, 101])
        self.assertEqual(result['unlinked_member_ids'], [103])
        self.assertEqual((result['member_count'], result['account_count']), (3, 3))
        self.assertEqual([(r['member_id'], r['player_tag']) for r in result['accounts']],
                         [(102, '#R0'), (101, '#P0'), (101, '#Q0')])

    async def test_selected_account_never_expands_to_owners_other_accounts(self):
        context = self.context()
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={
                '#P0': {'status': 'ok', 'data': {'name': 'Fresh Pilot'}}})):
            result = await identity_reads.member_accounts(context, {'account_tags': ['#P0']})
        self.assertEqual([r['player_tag'] for r in result['rows']], ['#P0'])
        self.assertEqual(result['members'][0]['account_tags'], ['#P0', '#Q0'])

    async def test_old_alias_and_spelling_tolerance_keep_unknown_owners_explicit(self):
        context = self.context()
        with patch.object(account_names, 'read_clash', AsyncMock(return_value={'items': []})):
            result = await account_names.resolve_account_names(context, ['Old Pilto'])
        self.assertEqual(list(result['accounts']), ['#P0'])
        self.assertEqual(result['accounts']['#P0']['member_id'], 101)

    async def test_link_argument_and_missing_setup_contracts(self):
        context = self.context()
        for ids in ([], [True], [0], [1] * 201, '101'):
            self.assertEqual(await read_linked_accounts(context, {'member_ids': ids}),
                             {'error': 'Choose between 1 and 200 member IDs.'})
        context.account_links.get_links_for_members.assert_not_called()
        result = await read_linked_accounts(replace(context, account_links=None), {'member_ids': [101]})
        self.assertEqual(result, {'error': 'Account links are not available.', 'cause': 'missing_setup'})

    async def test_large_cohort_uses_all_links_and_batches_standing_with_unknown_tiers(self):
        context = self.context()
        tags = ['#P' + bin(index + 1)[2:].replace('1', '2') for index in range(450)]
        rows = [{'player_tag': tag, 'discord_user_id': index + 1000,
                 'player_name_last_seen': 'Synthetic'} for index, tag in enumerate(tags)]
        async def summaries(selected):
            return {'rows': [{'player_tag': tag, 'ranked_league': (
                {'id': 1, 'name': 'Legend III'} if tag != tags[-1] else None),
                'legacy_league': {'name': 'Silver III'}, 'fields': {'bestTrophies': 6000},
                'coverage': {'status': 'partial'}} for tag in selected]}
        reader = SimpleNamespace(link_service=object(), current_links=AsyncMock(return_value={
            'rows': rows, 'coverage': {'complete': True}}), account_summary=AsyncMock(side_effect=summaries))
        context.bot.people_reads = reader
        forbidden = Mock(side_effect=AssertionError('Unexpected network or old link source'))
        context.bot.clash_client = SimpleNamespace(get=forbidden)
        context.bot.agent_model = SimpleNamespace(start_session=forbidden)
        context.account_links.get_links_for_members = forbidden
        result = await read_linked_accounts(context, {'member_ids': list(range(1000, 1451))}, maximum=10_000)
        self.assertEqual((result['account_count'], result['unlinked_member_ids']), (450, [1450]))
        profiles = await stored_profiles(context, tags)
        self.assertEqual([len(call.args[0]) for call in reader.account_summary.await_args_list], [200, 200, 50])
        projected = standing_rows(result['accounts'], profiles, 'legend')
        self.assertEqual(sum(row['matches'] is True for row in projected), 449)
        self.assertIsNone(projected[-1]['matches'])
        self.assertIsNone(projected[-1]['ranked_league'])
        self.assertEqual(projected[-1]['legacy_league'], 'Silver III')
        forbidden.assert_not_called()


    async def test_discord_name_with_failed_links_is_unknown_rather_than_unlinked(self):
        context = self.context()
        context.guild.members = [context.member]
        context.member.name = 'synthetic_person'
        context.member.display_name = 'Synthetic Person'
        context.account_links.get_links_for_members.side_effect = OSError('synthetic outage')
        result = await identity_reads.member_accounts(context, {'name': 'Synthetic Person', 'look_in': 'discord'})
        self.assertEqual(result['rows'][0]['profile_status'], 'links_unavailable')
        self.assertIsNone(result['members'][0]['has_elbow_helper_links'])
        self.assertIsNone(result['account_count'])
        self.assertFalse(result['count_complete'])
        self.assertIn({'source': 'bot_account_links', 'cause': 'source_unavailable'}, result['missing_sources'])
