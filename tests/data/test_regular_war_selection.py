"""Shared regular selections retain aliases, physical identities and final counts."""

import threading
from datetime import datetime, timedelta, timezone
from itertools import product
import unittest
from unittest.mock import patch

from elbow_helper.data.reads import WarReads
from elbow_helper.data.reads.war_selection import regular_id
from tests.data.record_fixtures import seed_war_alias
from tests.data.test_wars import END, SECOND, WarFixture, war

DAY = 86400 * SECOND


class RegularSelectionTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = WarReads(self.store)
        self.at = END + 90 * SECOND

    async def select(self, clans=('#PY',), **kwargs):
        return await self.reads.regular_selection(clans, as_of=END + 10 * DAY, **kwargs)

    async def test_aliases_and_record_ids_resolve_without_a_second_attendance_selector(self):
        await self.record(war('warEnded'))
        row, = await self.rows('SELECT id FROM wars')
        alias = 'REG:#PY:20261020T180000.000Z:#GR'
        await self.store.call(lambda c: (seed_war_alias(c, 'clan_health', alias, 'BEH', row['id'], at=self.at), c.commit()))
        source = await self.select(after=END - DAY, before=END + DAY)
        self.assertEqual(source['wars'][0]['aliases'], [alias, regular_id(source['wars'][0])])
        for ids in ((alias,), (str(row['id']),), tuple(source['wars'][0]['aliases'])):
            result = await self.select(war_ids=ids)
            self.assertEqual(result['rows'], source['rows'])
            self.assertEqual(len(result['wars']), 1)
        self.assertEqual((await self.select(war_ids=('CWL:#R0', 'unknown')))['wars'], [])

    async def test_latest_and_friendly_counts_deduplicate_physical_wars_across_clans(self):
        payload = war('warEnded', clans=('#PY', '#LQ'))
        await self.record(payload)
        await self.record({**payload, 'clan': payload['opponent'], 'opponent': payload['clan']}, scope='#LQ')
        other = war('warEnded', clans=('#PY', '#CU'), end='20261027T180000.000Z')
        other.update(preparationStartTime='20261025T180000.000Z', startTime='20261026T180000.000Z')
        await self.record(other)
        latest = await self.select(('#PY', '#LQ'), latest_completed=1)
        self.assertEqual(len(latest['wars']), 2)
        self.assertEqual(len({view['war_id'] for view in latest['wars']}), 1)
        self.assertEqual({row['player_tag'] for row in latest['rows']}, {'#P0', '#P2', '#Q0', '#Q2'})
        by_alias = await self.select(('#PY', '#LQ'), war_ids=(latest['wars'][0]['selection_id'],))
        self.assertEqual(by_alias['rows'], latest['rows'])
        for clan in ('#PY', '#LQ'):
            follow_up = await self.select((clan,), war_ids=(latest['wars'][0]['selection_id'],))
            self.assertEqual(len(follow_up['wars']), 1)
            self.assertEqual({row['clan_tag'] for row in follow_up['rows']}, {clan})
        friendly = war('warEnded', clans=('#PY', '#LQ'), end='20261030T180000.000Z')
        friendly.update(preparationStartTime='20261029T170000.000Z', startTime='20261029T180000.000Z')
        await self.record(friendly, at=END + 3 * DAY)
        source = await self.select(('#PY', '#LQ'), after=END - 2 * DAY, before=END + 4 * DAY)
        self.assertEqual(len({view['war_id'] for view in source['wars']}), 2)
        self.assertEqual(source['friendly_wars_not_counted'], 1)

    async def test_partial_and_stub_attacks_do_not_invent_final_misses_or_measurements(self):
        await self.record(war('warEnded'))
        await self.execute("UPDATE war_sides SET attacks=4 WHERE clan_tag='#PY'")
        await self.execute("UPDATE war_attacks SET extra_json='{\"stub\":true}' WHERE attack_order=1")
        source = await self.select(after=END - DAY, before=END + DAY)
        first, second = source['rows']
        self.assertIsNone(first['missed'])
        self.assertIsNone(second['missed'])
        self.assertIsNone(first['attacks_expected'])
        self.assertEqual(first['observed_attack_performance']['attacks'], 0)
        self.assertEqual(second['observed_attack_performance']['attacks'], 1)
        self.assertEqual(first['uncertainty_reasons'], ['attacks_incomplete'])

    async def test_boundaries_capacity_and_invalid_selections_fail_without_partial_facts(self):
        await self.record(war('warEnded'))
        self.assertEqual((await self.select(after=END - DAY, before=END))['rows'], [])
        self.assertEqual(len((await self.select(after=END, before=END + 1))['rows']), 2)
        for selection in ({}, {'after': END}, {'after': END, 'before': END},
                          {'latest_completed': 0}, {'latest_completed': True},
                          {'latest_completed': 1, 'war_ids': ('one',)}, {'war_ids': ('x',) * 57}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                await self.select(**selection)
        with patch('elbow_helper.data.reads.war_selection.MAX_SELECTED_WARS', 0), self.assertRaises(ValueError):
            await self.select(after=END, before=END + 1)

    async def test_selection_runs_in_one_worker_snapshot(self):
        await self.record(war('warEnded'))
        original, threads, transactions = self.reads._snapshot, [], []

        def snapshot(connection, operation, args):
            threads.append(threading.get_ident())
            connection.set_trace_callback(transactions.append)
            return original(connection, operation, args)

        with patch.object(self.reads, '_snapshot', side_effect=snapshot):
            await self.select(after=END - DAY, before=END + DAY)
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(sum(sql.startswith('BEGIN') for sql in transactions), 1)

    async def test_compact_participation_retains_more_than_one_page(self):
        tags = ['#P' + ''.join(parts) for parts in product('0289', repeat=3)][:50]
        for index in range(5):
            end = datetime(2026, 10, 20, 18, tzinfo=timezone.utc) + timedelta(days=index * 2)
            payload = war('warEnded', end=end.strftime('%Y%m%dT%H%M%S.000Z'))
            payload.update(teamSize=50,
                preparationStartTime=(end - timedelta(days=2)).strftime('%Y%m%dT%H%M%S.000Z'),
                startTime=(end - timedelta(days=1)).strftime('%Y%m%dT%H%M%S.000Z'))
            # Final counts without attack detail: every account used zero attacks.
            payload['clan'].update(attacks=0, members=[
                {'tag': tag, 'name': tag, 'townhallLevel': 16, 'mapPosition': position}
                for position, tag in enumerate(tags, 1)])
            await self.record(payload, at=int(end.timestamp()) * SECOND + 90 * SECOND)
        source = await self.select(after=END - 10 * DAY, before=END + DAY)
        self.assertEqual((len(source['wars']), len(source['rows'])), (5, 250))
        self.assertEqual({row['missed'] for row in source['rows']}, {2})


if __name__ == '__main__':
    unittest.main()
