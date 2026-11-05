"""The unified agent people reads use guarded record facts without network I/O."""

from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.reads.people import (
    PeopleAdapter, PeopleSelection, account_histories, read_people_links, stored_profiles,
)
from elbow_helper.data.reads import PeopleReads
from elbow_helper.infrastructure.persistence.migrations import UnsupportedSQLiteVersionError
from agent.engine.test_agent_plan_flow import _context
from .record_fixtures import PeopleFixture, row


class PeopleAgentReadTests(PeopleFixture):
    def context(self):
        forbidden = Mock(side_effect=AssertionError('Unexpected question-time I/O'))
        self.reader = PeopleReads(self.store, link_service=self.service)
        context = replace(_context(), bot=SimpleNamespace(
            people_reads=self.reader, clash_client=SimpleNamespace(get=AsyncMock(side_effect=forbidden)),
            agent_model=SimpleNamespace(start_session=forbidden)), account_links=SimpleNamespace(
                get_links_for_members=forbidden, get_links_by_tags=forbidden, get_all_links=forbidden),
            cwl_queries=SimpleNamespace(account_names=forbidden))
        return context, forbidden

    async def test_shared_current_selection_matches_projection_after_local_operation(self):
        await self.service.upsert_link(**row('#V0', 100, 'Added alt'), at=300)
        await self.service.upsert_link(**row('#P0', 300, 'New owner'), at=400)
        context, forbidden = self.context()
        result = await read_people_links(context, {'member_ids': [300, 999, 100]})
        self.assertEqual(result['unlinked_member_ids'], [999])
        self.assertEqual([(r['member_id'], r['player_tag']) for r in result['accounts']],
                         [(300, '#P0'), (100, '#Q0'), (100, '#V0')])
        selected = await PeopleAdapter(context).links(PeopleSelection('accounts', ('#P0', '#G0')))
        self.assertEqual(set(selected.by_tag), {'#P0', '#G0'})
        self.assertIsNone(selected.by_tag['#G0'])
        self.assertEqual([r['player_tag'] for r in selected.by_member[300]], ['#P0'])
        history = await account_histories(context, ['#P0'])
        self.assertEqual([r['member_id'] for r in history['#P0']['ownership_intervals']], [300, 100])
        self.assertFalse(history['#P0']['first_observed_is_first_ever'])
        forbidden.assert_not_called()

    async def test_recovery_conflicts_never_fall_back_to_other_sources(self):
        context, forbidden = self.context()
        await self.execute("INSERT INTO link_operations(operation_key,requested_json,before_json,after_json,"
                           "before_digest,after_digest,created_at,status) "
                           "VALUES ('pending','{}','{}','{}','before','after',250,'pending')")
        self.assertEqual((await read_people_links(context, {'member_ids': [100]}))['cause'], 'source_unavailable')
        forbidden.assert_not_called()

    async def test_record_failures_stay_unknown_and_access_is_checked_after_reads(self):
        context, forbidden = self.context()
        for error in (OSError('disk'), sqlite3.OperationalError('locked'), TimeoutError(),
                      UnsupportedSQLiteVersionError('future schema')):
            with self.subTest(error=error), patch.object(self.reader, 'current_links', AsyncMock(side_effect=error)):
                self.assertEqual((await read_people_links(context, {'member_ids': [100]}))['cause'], 'source_unavailable')
            with self.subTest(error=error), patch.object(self.reader, 'account_summary', AsyncMock(side_effect=error)):
                self.assertIsNone(await stored_profiles(context, ['#P0']))
            with self.subTest(error=error), patch.object(self.reader, 'directory_names', AsyncMock(side_effect=error)):
                self.assertIsNone(await PeopleAdapter(context).directory_names())
        with patch('elbow_helper.agent.reads.people.require_evidence_access',
                   AsyncMock(side_effect=[None, PermissionError('access lost')])):
            with self.assertRaises(PermissionError):
                await PeopleAdapter(context).links(PeopleSelection('members', (100,)))
        forbidden.assert_not_called()

    async def test_directory_union_matches_feature_names_without_calling_feature(self):
        from elbow_helper.data.reads import CwlReads, StayReads
        from .link_fixtures import CLANS
        from .test_wars import war, END
        from .test_family_store import response
        from elbow_helper.data.store.wars import record_response
        clan_tag = next(iter(CLANS.values())).tag
        payload = war('warEnded', clans=(clan_tag, '#GR'))
        payload['clan']['members'][0]['name'] = 'Old lineup alias'
        run = await self.store.begin_run('wars', 'family_wars', [('current_war', clan_tag)], END)
        await self.store.call(record_response, run, 0, 'current_war', clan_tag, response(payload),
                              {clan.tag for clan in CLANS.values()}, END)
        await self.store.finish(run)
        # Exercise the two neutral inputs directly: this is the feature's union.
        stays = await StayReads(self.store).names(as_of=END)
        wars = await CwlReads(self.store).account_names([clan.tag for clan in CLANS.values()], as_of=END)
        expected = sorted({(r['player_tag'], r['player_name']) for r in stays['rows'] + wars['rows']})
        names = await PeopleReads(self.store, clans=CLANS.values()).directory_names(END)
        self.assertEqual([(r['player_tag'], r['player_name']) for r in names['rows']], expected)
        self.assertIn(('#P0', 'Old lineup alias'), expected)
        self.assertNotIn('#Q0', {r['player_tag'] for r in wars['rows']})  # Opponent names never enter the directory.
