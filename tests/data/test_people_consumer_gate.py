"""Dated ownership reads never pass a deferred switch or pending operation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.record_identity import account_histories
from elbow_helper.data.reads import PeopleReads

from .test_people_import import _PeopleFixture, row


class PeopleConsumerGateTests(_PeopleFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        access = patch('elbow_helper.agent.record_identity.require_evidence_access', AsyncMock())
        access.start()
        self.addCleanup(access.stop)

    async def test_dated_ownership_includes_completed_local_operations(self):
        await self.service.startup(at=200)
        people = PeopleReads(self.store, link_service=self.service)
        context = SimpleNamespace(bot=SimpleNamespace(people_reads=people))
        result = await account_histories(context, ['#P0', '#Q0'])
        self.assertEqual(result['#P0']['ownership_intervals'][0]['member_id'], 100)
        self.assertEqual(result['#Q0']['legacy_name'], '')
        await self.service.upsert_link(**row('#V0', 300, 'Synthetic new account'), at=300)
        result = await account_histories(context, ['#V0'])
        self.assertEqual(result['#V0']['ownership_intervals'][0]['member_id'], 300)
        self.assertEqual(result['#V0']['ownership_intervals'][0]['basis'], 'local_operation')

    async def test_deferred_and_failed_operations_never_publish_ownership(self):
        people = PeopleReads(self.store, link_service=self.service)
        context = SimpleNamespace(bot=SimpleNamespace(people_reads=people))
        self.assertEqual(await account_histories(context, ['#P0']), {})
        await self.service.startup(at=200)
        await self.execute("CREATE TRIGGER refuse_operation BEFORE INSERT ON link_operations "
                           "BEGIN SELECT RAISE(ABORT,'refused'); END")
        with self.assertRaises(Exception):
            await self.service.upsert_link(**row('#V0', 300, 'Synthetic new account'), at=300)
        result = await people.account_history(['#V0'])
        self.assertIsNone(result['rows'][0]['record_history'])
        self.assertEqual(await self.rows("SELECT * FROM link_operations"), [])
        aliases = await people.resolve('Player')
        self.assertTrue(any(r['kind'] == 'account' for r in aliases['rows']))
