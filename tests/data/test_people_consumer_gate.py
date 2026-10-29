"""Dated ownership reads never pass a deferred switch or pending operation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.record_identity import account_histories
from elbow_helper.data.reads import PeopleReads
from elbow_helper.data.store.legacy_links import LinkConflict

from .test_people_import import _PeopleFixture, ProcessCrash, row


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

    async def test_deferred_and_pending_operations_omit_ownership_history(self):
        people = PeopleReads(self.store, link_service=self.service)
        context = SimpleNamespace(bot=SimpleNamespace(people_reads=people))
        self.assertEqual(await account_histories(context, ['#P0']), {})
        await self.service.startup(at=200)
        with patch.object(self.service, '_finish', side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.service.upsert_link(**row('#V0', 300, 'Synthetic new account'), at=300)
        with self.assertRaises(LinkConflict):
            await people.account_history(['#V0'])
        self.assertEqual(await account_histories(context, ['#V0']), {})
        # Historical names do not depend on the current ownership switch.
        aliases = await people.resolve('Player')
        self.assertTrue(any(r['kind'] == 'account' for r in aliases['rows']))
