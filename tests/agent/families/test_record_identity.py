"""Dated record facts accompany the existing live identity and name reads."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from elbow_helper.agent import record_identity
from elbow_helper.agent.capabilities.account_links.reads import read_linked_accounts
from elbow_helper.agent.datasets import membership as history_tool
from elbow_helper.agent.families import identity_reads, account_names, model_view
from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import PeopleReads, MembershipReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import mirror_links, observe_discord
from elbow_helper.data.store.players import collect_player
from elbow_helper.infrastructure.persistence.migrations import UnsupportedSQLiteVersionError
from agent.engine.test_agent_plan_flow import _context
from data.test_family_store import player, response
from data.test_players import profile


def iso(at):
    return datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat()


class RecordConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.store = FamilyStore(Path(temporary.name) / 'family.sqlite3')
        await self.store.initialize()
        self.at = int(datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp() * 1_000_000)
        self.initial_at = self.at
        self.clock = patch('elbow_helper.data.reads.core.now_us', lambda: self.at + 10)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.codes = list(CLANS)[:2]
        self.role = SimpleNamespace(id=CLANS[self.codes[0]].member_role_id, name='Synthetic home', is_default=lambda: False)
        self.context = _context()
        for role in self.context.member.roles:
            role.name, role.is_default = 'Synthetic access', lambda: False
        self.target = SimpleNamespace(id=100, name='synthetic_owner', display_name='Synthetic Owner',
            global_name=None, nick='Synthetic Pilot', bot=False, roles=[self.role],
            joined_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.empty = SimpleNamespace(**{**vars(self.target), 'id': 300, 'name': 'empty_member',
                                       'nick': None, 'display_name': 'Empty Member', 'roles': []})
        self.context.guild.members = [self.context.member, self.target, self.empty]
        self.context.guild.get_member = lambda identifier: next((m for m in self.context.guild.members if m.id == identifier), None)
        self.context.guild.roles += [self.role]
        self.links = {tag: {'player_tag': tag, 'discord_user_id': 100,
            'player_name_last_seen': name, 'last_seen_clan_tag': '',
            'last_seen_clan_code': None, 'last_seen_role': None}
            for tag, name in (('#P0', 'Synthetic Main'), ('#Q0', 'Synthetic Alt'))}
        self.legacy = SimpleNamespace(
            get_all_links=Mock(side_effect=lambda: deepcopy(self.links)),
            get_links_for_members=Mock(side_effect=lambda ids: {i: [deepcopy(r) for r in self.links.values()
                                                       if r['discord_user_id'] == i] for i in ids}),
            get_links_by_tags=Mock(side_effect=lambda tags: {t: deepcopy(self.links.get(t)) for t in tags}))
        self.people = PeopleReads(self.store, alias_reader=lambda: [{'player_tag': '#Q0', 'player_name': 'Old Synthetic Alias'}])
        self.membership = MembershipReads(self.store)
        self.bot = SimpleNamespace(people_reads=self.people, membership_reads=self.membership, paths=None)
        self.context = replace(self.context, bot=self.bot, account_links=self.legacy,
            cwl_queries=SimpleNamespace(account_names=self.people.alias_reader))
        self.live_clans = {}
        await self.observe_clans()
        run = await self.store.begin_run('links', 'current_links', [('account_links', 'current')], self.at)
        await self.store.call(mirror_links, run, self.links, self.at)
        await self.store.finish(run)
        await self.observe_discord()
        self.payloads = {}
        for tag, name in (('#P0', 'Synthetic Main'), ('#Q0', 'Synthetic Alt')):
            payload = profile(tag)
            payload.update(name=name, townHallLevel=16, role='member')
            if tag == '#P0':
                payload['clan'] = {'tag': CLANS[self.codes[0]].tag, 'name': CLANS[self.codes[0]].name}
            else:
                payload.pop('clan', None)
            self.payloads[tag] = payload
            await self.observe_profile(tag, payload)

    async def observe_clans(self, rosters=None, failed=None):
        rosters = rosters or {self.codes[0]: [player(name='Synthetic Main', donations=12, donationsReceived=9)],
                              self.codes[1]: [player('#R0', name='Synthetic Visitor', role='admin', donations=4)]}
        responses = []
        for clan in CLANS.values():
            data = {'tag': clan.tag, 'name': clan.name, 'members': len(rosters.get(clan.code, [])),
                    'type': 'inviteOnly', 'requiredTownhallLevel': 12, 'requiredTrophies': 1000,
                    'requiredBuilderBaseTrophies': 0, 'memberList': rosters.get(clan.code, [])}
            self.live_clans[clan.tag] = deepcopy(data)
            responses.append(response(None, 503) if clan.code == failed else response(data))
        specs = [(kind, c.tag) for c in CLANS.values() for kind in ('clan_profile', 'clan_roster')]
        run = await self.store.begin_run('clans', 'family_clans', specs, self.at)
        await self.store.call(collect_clans, run, tuple(CLANS.values()), responses, self.at)
        await self.store.finish(run)

    async def observe_discord(self, complete=True):
        run = await self.store.begin_run('discord', 'guild', [('discord_scan', 'guild')], self.at)
        rows = [{'id': m.id, 'username': getattr(m, 'name', m.display_name), 'display_name': m.display_name,
                 'nick': getattr(m, 'nick', None), 'bot': False, 'joined_at': self.at - 100,
                 'roles': [r.id for r in m.roles]} for m in self.context.guild.members]
        await self.store.call(observe_discord, run, rows, self.at, complete=complete)
        await self.store.finish(run)

    async def observe_profile(self, tag, payload):
        identifier = await self.store.call(lambda c: c.execute('SELECT id FROM accounts WHERE player_tag=?', (tag,)).fetchone()[0])
        run = await self.store.begin_run('players', 'accounts', [('player_profile', tag)], self.at)
        await self.store.call(collect_player, run, 0, identifier, tag, response(payload), self.at)
        await self.store.finish(run)

    async def live(self, context, arguments):
        source = self.payloads if arguments['kind'] == 'player' else self.live_clans
        return {'items': [{'tag': tag, 'status': 'ok', 'observed_at': iso(self.at),
                 'data': {**source[tag], **({'clan_status': 'in_clan' if source[tag].get('clan') else 'not_in_clan'}
                                          if arguments['kind'] == 'player' else {})}}
                          for tag in arguments['tags']]}

    def without_record(self):
        return replace(self.context, bot=SimpleNamespace())

    async def test_membership_history_parity_transfers_returns_and_pagination(self):
        first = self.at
        snapshots = [(first, {self.codes[0]: [player(name='Synthetic Main')]})]
        for roster in ({}, {self.codes[1]: [player(name='Synthetic Main', role='admin')]},
                       {self.codes[0]: [player(name='Synthetic Main')]}):
            self.at += 900_000_000
            await self.observe_clans(roster or {self.codes[0]: []})
            snapshots.append((self.at, roster))
        after, before = iso(first + 1), iso(self.at + 10)
        arguments = {'after': after, 'before': before, 'history_after': iso(first), 'limit': 1}
        runs = [{'run_id': i, 'created_ts': at / 1_000_000} for i, (at, _) in enumerate(snapshots)]
        rows = [{'run_id': i, 'clan_code': code, 'player_tag': p['tag'], 'player_name': p['name']}
                for i, (_, roster) in enumerate(snapshots) for code, players in roster.items() for p in players]
        # The initial fixture also contains a visitor in the second clan.
        rows.append({'run_id': 0, 'clan_code': self.codes[1], 'player_tag': '#R0', 'player_name': 'Synthetic Visitor'})
        legacy = AsyncMock(return_value=(runs, rows, False))
        with patch.object(history_tool, 'read_observations', Mock(return_value=(runs, rows, False))), \
             patch.object(history_tool, 'SOURCES', [SimpleNamespace(alias='health', resolve=lambda _: Path('unused'))]):
            legacy_context = replace(self.without_record(), bot=SimpleNamespace(paths=None))
            before_result = await history_tool.read_membership_history(legacy_context, arguments)
        with patch.object(history_tool, 'read_observations', legacy):
            after_result = await history_tool.read_membership_history(self.context, arguments)
        legacy.assert_not_called()
        self.assertEqual(after_result, before_result)
        self.assertIsNotNone(after_result['next_offset'])
        self.assertEqual(after_result['events'][0]['kind'], 'family_transfer')

    async def test_history_before_record_started_uses_existing_snapshots(self):
        arguments = {'after': iso(self.at - 1_000_000),
                     'before': iso(self.at + 10), 'history_after': iso(self.at - 2_000_000)}
        fallback = Mock(return_value=([], [], False))
        with patch.object(history_tool, 'read_observations', fallback), \
             patch.object(history_tool, 'SOURCES', [SimpleNamespace(alias='health', resolve=lambda _: Path('unused'))]):
            result = await history_tool.read_membership_history(self.context, arguments)
        fallback.assert_called_once()
        self.assertEqual(result['events'], [])

    async def test_record_alias_remains_searchable_with_live_current_name(self):
        self.at += 86_400_000_000
        await self.observe_clans({self.codes[0]: [player(name='Record only alias')]})
        self.at += 121_000_000
        self.live_clans[CLANS[self.codes[0]].tag]['memberList'][0]['name'] = 'Live renamed main'
        with patch.object(account_names, 'read_clash', AsyncMock(side_effect=self.live)) as live:
            result = await account_names.resolve_names(self.context, ['Record only alias'], 'game')
        live.assert_awaited_once()
        self.assertTrue(result['selection_complete'])
        self.assertEqual(result['accounts']['#P0']['player_name'], 'Live renamed main')
        self.assertIn('Record only alias', result['accounts']['#P0']['names'])

    async def test_current_profiles_always_use_live_and_keep_dated_facts(self):
        original = (await self.people.account_history(['#P0']))['rows'][0]['record_history']
        self.payloads['#P0'].pop('clan')
        self.payloads['#P0'].update(name='Live name', role=None, townHallLevel=17, leagueTier={'id': 99})
        live = AsyncMock(side_effect=self.live)
        with patch.object(identity_reads, 'read_clash', live):
            result = await identity_reads.member_accounts(self.context, {'account_tags': ['#P0'], 'include_history': True})
            cohort = await identity_reads.member_cohort(self.context, {
                'basis': 'role_holders', 'role_ids': [self.role.id], 'include_accounts': True, 'include_history': True})
        self.assertEqual(live.await_count, 2)
        self.legacy.get_links_by_tags.assert_called_once_with(['#P0'])
        self.assertEqual(self.legacy.get_links_for_members.call_args_list, [call([100]), call([100])])
        self.assertEqual([m['member_id'] for m in result['members']], [100])
        self.assertNotIn('home_clans', result['rows'][0])
        row = result['rows'][0]
        self.assertEqual((row['clan_status'], row['clan'], row['role']), ('not_in_clan', None, None))
        self.assertEqual((row['player_name'], row['townHallLevel'], row['ranked_league']['id']), ('Live name', 17, 99))
        self.assertEqual(row['record_history'], original)
        self.assertEqual(original['ownership_intervals'][0]['member_id'], 100)
        self.assertEqual(original['clan_intervals'][0]['clan_tag'], CLANS[self.codes[0]].tag)
        self.assertEqual(original['first_observed_at'], self.initial_at)
        self.assertEqual(original['last_observed_at'], self.initial_at)
        self.assertEqual(original['legacy_name'], 'Synthetic Main')
        account = next(a for r in cohort['rows'] for a in r['accounts'] if a['player_tag'] == '#P0')
        self.assertEqual(model_view.account_view(account, nested=True)['record_history'], original)

    async def test_current_rosters_members_and_links_keep_the_existing_sources(self):
        self.target.roles = []  # Gateway is ahead of the dated scan.
        self.links['#P0']['discord_user_id'] = 300  # Current links are ahead of the record.
        clan = CLANS[self.codes[0]].tag
        self.live_clans[clan]['memberList'][0].update(name='Live roster name', role='admin')
        arguments = {'clan_codes': self.codes, 'basis': 'current_in_game'}
        live = AsyncMock(side_effect=self.live)
        with patch.object(identity_reads, 'read_clash', live):
            roster = await identity_reads.clan_rosters(self.context, arguments)
            cohort = await identity_reads.member_cohort(self.context, {'basis': 'role_holders', 'role_ids': [self.role.id]})
        live.assert_awaited_once()
        row = next(r for r in roster['rows'] if r['player_tag'] == '#P0')
        self.assertEqual((row['member_id'], row['player_name'], row['role']), (300, 'Live roster name', 'admin'))
        self.assertEqual(cohort['rows'], [])
        linked = await read_linked_accounts(self.context, {'member_ids': [100, 300]})
        self.assertEqual(next(r['member_id'] for r in linked['accounts'] if r['player_tag'] == '#P0'), 300)
        self.legacy.get_links_for_members.assert_called_once_with([100, 300])

    async def test_missing_or_unavailable_history_keeps_live_current_answer(self):
        with patch.object(identity_reads, 'read_clash', self.live):
            expected = await identity_reads.member_accounts(self.without_record(), {'member_ids': [100]})
        for error in (OSError, UnsupportedSQLiteVersionError):
            with self.subTest(error=error), patch.object(self.people, 'account_history', AsyncMock(side_effect=error('synthetic'))), \
                 patch.object(identity_reads, 'read_clash', self.live):
                actual = await identity_reads.member_accounts(self.context, {'member_ids': [100]})
            self.assertEqual(actual, expected)

    async def test_history_is_read_only_when_requested(self):
        history = AsyncMock(wraps=self.people.account_history)
        with patch.object(self.people, 'account_history', history), patch.object(identity_reads, 'read_clash', self.live):
            plain = await identity_reads.member_accounts(self.context, {'account_tags': ['#P0']})
            plain_cohort = await identity_reads.member_cohort(self.context, {
                'basis': 'role_holders', 'role_ids': [self.role.id], 'include_accounts': True})
            history.assert_not_awaited()
            asked = await identity_reads.member_accounts(self.context, {'account_tags': ['#P0'], 'include_history': True})
            asked_cohort = await identity_reads.member_cohort(self.context, {
                'basis': 'role_holders', 'role_ids': [self.role.id], 'include_accounts': True, 'include_history': True})
            history.assert_awaited()
        self.assertNotIn('record_history', plain['rows'][0])
        self.assertTrue(all('record_history' not in a for r in plain_cohort['rows'] for a in r['accounts']))
        self.assertIn('record_history', asked['rows'][0])
        self.assertTrue(any('record_history' in a for r in asked_cohort['rows'] for a in r['accounts']))

    async def test_live_failure_keeps_history_without_record_current_fields(self):
        for live_response in ({'error': 'synthetic outage', 'cause': 'unavailable'},
                              {'items': [{'tag': '#P0', 'status': 'not_found'}]}, {'items': []}):
            with self.subTest(response=live_response), patch.object(identity_reads, 'read_clash', AsyncMock(return_value=live_response)):
                row = (await identity_reads.member_accounts(
                    self.context, {'account_tags': ['#P0'], 'include_history': True}))['rows'][0]
            self.assertIn(row['profile_status'], ('not_found', 'unavailable'))
            self.assertIsNone(row['clan'])
            self.assertIsNone(row['townHallLevel'])
            self.assertNotIn('clan_status', row)
            self.assertIn('record_history', row)

    async def old_shared_name(self):
        self.at += 1_000_000
        await self.observe_clans({self.codes[0]: [player(tag, name='Shared old alias') for tag in ('#P0', '#Q0')]})
        for row in self.live_clans[CLANS[self.codes[0]].tag]['memberList']:
            row['name'] = 'Live name ' + row['tag']

    async def test_same_owner_alias_over_two_accounts_resolves_without_expanding_alts(self):
        await self.old_shared_name()
        self.links['#V0'] = {**self.links['#P0'], 'player_tag': '#V0', 'player_name_last_seen': 'Unrelated third alt'}
        with patch.object(account_names, 'read_clash', self.live):
            result = await account_names.resolve_names(self.context, ['Shared old alias'], 'game')
        self.assertTrue(result['selection_complete'])
        self.assertEqual(result['member_ids'], [100])
        self.assertEqual(set(result['accounts']), {'#P0', '#Q0'})
        resolution = result['name_resolutions'][0]
        self.assertEqual((resolution['status'], resolution['found_in']), ('resolved', 'game'))
        self.assertTrue(all('family_record' in r['sources'] for r in resolution['candidates']))

    async def test_alias_ambiguity_uses_current_owners_without_discord_collisions(self):
        await self.old_shared_name()
        for owner in (300, None, 100):
            with self.subTest(owner=owner):
                if owner is None:
                    self.links.pop('#Q0')
                else:
                    self.links['#Q0'] = {**self.links['#P0'], 'player_tag': '#Q0', 'discord_user_id': owner}
                self.empty.nick = 'Shared old alias' if owner == 100 else None
                with patch.object(account_names, 'read_clash', self.live):
                    result = await account_names.resolve_names(self.context, ['Shared old alias'], 'game')
                self.assertTrue(result['selection_complete'])
                resolution = result['name_resolutions'][0]
                self.assertEqual(resolution['found_in'], 'game')
                self.assertEqual(resolution['matching_members'], [])
                self.assertEqual(resolution['status'], 'resolved' if owner == 100 else 'ambiguous')
                self.assertEqual(result['resolved_member_ids'], [100] if owner == 100 else [])

    async def test_alias_record_outage_keeps_existing_name_sources(self):
        with patch.object(self.people, 'resolve', AsyncMock(side_effect=OSError('synthetic outage'))), \
             patch.object(account_names, 'read_clash', self.live):
            result = await account_names.resolve_names(self.context, ['Synthetic Main'], 'game')
        self.assertTrue(result['selection_complete'])
        self.assertEqual(result['member_ids'], [100])

    async def test_history_and_alias_reads_recheck_evidence_access(self):
        for method, operation, arguments in (('account_history', record_identity.account_histories, ['#P0']),
                                             ('resolve', record_identity.aliases, ['Synthetic Main'])):
            source = AsyncMock(wraps=getattr(self.people, method))
            for denied_call in (1, 2):
                calls = 0
                async def access(_):
                    nonlocal calls
                    calls += 1
                    if calls == denied_call:
                        raise PermissionError('synthetic access loss')
                source.reset_mock()
                with patch.object(self.people, method, source), patch.object(record_identity, 'require_evidence_access', access):
                    with self.assertRaises(PermissionError):
                        await operation(self.context, arguments)
                self.assertEqual(source.await_count, denied_call - 1)

    async def test_history_record_outage_uses_existing_membership_snapshots(self):
        arguments = {'after': iso(self.at + 1), 'before': iso(self.at + 10), 'history_after': iso(self.at)}
        fallback = Mock(return_value=([], [], False))
        with patch.object(self.membership, 'generations', AsyncMock(side_effect=OSError('synthetic outage'))), \
             patch.object(history_tool, 'read_observations', fallback), \
             patch.object(history_tool, 'SOURCES', [SimpleNamespace(alias='health', resolve=lambda _: Path('unused'))]):
            result = await history_tool.read_membership_history(self.context, arguments)
        fallback.assert_called_once()
        self.assertEqual(result['events'], [])
