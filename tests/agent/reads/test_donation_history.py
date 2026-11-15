"""Donations per member per month for one clan, from stored donation samples and polling (synthetic).

The old health file's hourly captures, its clan stays and counter samples are written into a family
record. Unchanged counters are not stored, as the record keeps them. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
from itertools import islice, product
from types import SimpleNamespace
import unittest

from elbow_helper.agent.reads.donations import TOOLS
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from tests.agent.engine.test_agent_plan_flow import _context
from tests.data.test_health_reads import BITS, CLAN, HOUR, OTHER, RecordFixture

US = 1_000_000


def at(month, day, hour=0):
    return int(datetime(2026, month, day, hour, tzinfo=timezone.utc).timestamp()) * US


class DonationHistoryTests(RecordFixture):
    async def test_clan_average_uses_all_accounts_before_paging_or_member_grouping(self):
        from elbow_helper.agent.engine.result_hints import RESULT_FIELDS, RETURN_HINTS
        from elbow_helper.agent.reads.snapshots import FamilyReport

        await self.sql("UPDATE clans SET family_code='BEM' WHERE tag=?", (CLAN,))
        tags = ['#PP' + ''.join(chars) for chars in islice(product('0289PYLQGRJCUV', repeat=2), 74)]
        start, end = at(10, 31), at(11, 14)
        for index, tag in enumerate(tags):
            account = await self.account(tag)
            await self.stay(account, CLAN, start, last=end)
            if index == 73:
                continue  # Unknown remains outside the denominator; the 71 known zeros count.
            for moment, given in ((start, 0), (at(11, 2), 100 if index == 0 else 50 if index == 1 else 0)):
                await self.sql('INSERT INTO donation_snapshots(account_id,sampled_at,clan_id,donations,received,'
                    "field_presence_json,source_item_id) VALUES (?,?,?,?,?,'3',?)",
                    (account, moment, self.clans[CLAN], given, given // 2, self.item))
        links = SimpleNamespace(get_links_by_tags=lambda requested: {
            tag: {'discord_user_id': 41} for tag in requested})
        context = replace(_context(), account_links=links,
                          bot=SimpleNamespace(activity_reads=self.activity, stay_reads=self.stays))
        expected = {'coverage': 'P', 'known_account_count': 73, 'unknown_account_count': 1,
                    'donations': 150, 'donations_received': 75,
                    'average_donations': 150 / 73, 'average_donations_received': 75 / 73}
        for unit in ('account', 'member'):
            for by_month in (False, True):
                arguments = {'clan_codes': ['BEM'], 'after': '2026-11-01T00:00:00+00:00',
                             'before': '2026-11-15T06:07:43+00:00', 'unit': unit,
                             'by_month': by_month, 'limit': 10}
                result = await TOOLS[0].handler(context, arguments)
                with self.subTest(unit=unit, by_month=by_month):
                    self.assertNotIn('error', result)
                    self.assertEqual(result['returned_rows'], 10 if unit == 'account' else 1)
                    summary, = result['clan_totals']
                    self.assertEqual(summary['account_count'], 74)
                    self.assertEqual(summary['whole_period'], expected)
                    self.assertEqual(summary['selected_clans'], expected)
                    self.assertEqual(summary['after'], arguments['after'])
                    self.assertEqual(summary['before'], arguments['before'])
                    self.assertEqual(summary.get('month'), '2026-11' if by_month else None)
                    report = context.state.reports[result['report_id']]
                    self.assertIsInstance(report, FamilyReport)
                    self.assertEqual(report.page(10, 10)['clan_totals'], result['clan_totals'])
                    self.assertIn('unknown accounts', result['average_basis'])
        self.assertIn('average_donations', RESULT_FIELDS['read_donation_history']['clan_totals'][0]['selected_clans'])
        self.assertIn('clan_totals', RETURN_HINTS['read_donation_history'])

    async def test_clan_ranking_keeps_family_totals_and_counts_a_higher_first_counter_after_a_move(self):
        await self.sql("UPDATE clans SET family_code='BEH' WHERE tag=?", (CLAN,))
        mover, resident = [await self.account(tag) for tag in ('#PP29L', '#PP2L')]
        start, end, move = at(10, 31), at(11, 14), at(11, 9)
        await self.stay(mover, OTHER, start, move, last=move - HOUR, end_previous=move - HOUR)
        await self.stay(mover, CLAN, move, last=end)
        await self.stay(resident, CLAN, start, last=end)
        for account, samples in (
                (mover, ((start, OTHER, 0, 0), (at(11, 2), OTHER, 1000, 1000),
                         (at(11, 8), OTHER, 5, 2), (move, CLAN, 20, 8), (at(11, 10), CLAN, 30, 15))),
                (resident, ((start, CLAN, 10, 10), (at(11, 3), CLAN, 90, 60),
                            (at(11, 11), CLAN, 110, 80)))):
            for moment, clan, given, received in samples:
                await self.sql('INSERT INTO donation_snapshots(account_id,sampled_at,clan_id,donations,received,'
                    "field_presence_json,source_item_id) VALUES (?,?,?,?,?,'3',?)",
                    (account, moment, self.clans[clan], given, received, self.item))
        # Complete roster polling; the mover's selected-clan polling starts only on arrival.
        def polls(connection):
            with sqlite_transaction(connection):
                connection.executemany('INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,'
                    'started_at,fetched_at,completeness_json) VALUES (?,?,\'clan_roster\',?,\'ok\',?,?,?)',
                    [(self.players, moment + self.clans[clan], clan, moment, moment, '{"complete":true}')
                     for clan in (CLAN, OTHER) for moment in range(start, end + 1, HOUR // 4)])
        await self.store.call(polls)
        links = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {'discord_user_id': 41 if tag == '#PP29L' else 42} for tag in tags})
        context = replace(_context(), account_links=links,
                          bot=SimpleNamespace(activity_reads=self.activity, stay_reads=self.stays))
        window = {'after': '2026-11-01T00:00:00+00:00', 'before': '2026-11-14T00:00:00+00:00'}
        for unit in ('account', 'member'):
            for by_month in (False, True):
                arguments = {**window, 'unit': unit, 'by_month': by_month}
                family = await TOOLS[0].handler(context, {**arguments, 'account_tags': ['#PP29L', '#PP2L']})
                selected = await TOOLS[0].handler(context, {**arguments, 'clan_codes': ['BEH']})
                with self.subTest(unit=unit, by_month=by_month):
                    self.assertEqual([row['member_id'] for row in family['rows']], [41, 42])
                    self.assertEqual([row['member_id'] for row in selected['rows']], [42, 41])
                    self.assertEqual([(r['clan_donations'], r['clan_donations_received'], r['clan_coverage'])
                                      for r in selected['rows']], [(100, 70, 'C'), (30, 15, 'P')])
                    self.assertEqual([{k: v for k, v in row.items() if not k.startswith('clan_')}
                                      for row in reversed(selected['rows'])], family['rows'])
                    self.assertEqual([(r['donations'], r['donations_received'], r['coverage'])
                                      for r in family['rows']], [(1030, 1015, 'C'), (100, 70, 'C')])

    async def captures(self, account, times):
        """One changed counter sample at each old capture, so the capture times are on record."""
        presence = json.dumps({"fields": BITS["donations"] | BITS["donationsReceived"]})

        def write(connection):
            with sqlite_transaction(connection):
                connection.executemany(
                    "INSERT INTO player_counters(account_id,sampled_at,donations,received,achievement_values_json,"
                    "transient_item_fields_json,field_presence_json,reset_context_json,source_run_id) "
                    "VALUES (?,?,?,0,'{}','{}',?,'{}',?)",
                    [(account, moment, index, presence, self.legacy) for index, moment in enumerate(times)])
        await self.store.call(write)

    async def test_clan_donations_per_member_per_month_mark_an_unpolled_reset_partial(self):
        await self.sql("UPDATE clans SET family_code='BEH' WHERE tag=?", (CLAN,))
        busy, steady, quiet, gapped = [await self.account(tag) for tag in ("#PP8", "#PP29L", "#PP2L", "#PP29Q")]
        start, end = at(7, 31), at(10, 1)
        # The old file captured hourly, except for a six-hour outage on 20 August.
        outage = range(at(8, 20), at(8, 20, 7))
        await self.captures(busy, [moment for moment in range(start, end + 1, HOUR) if moment not in outage])
        await self.old_stay(busy, OTHER, start, end)
        for account in (steady, quiet, gapped):
            await self.old_stay(account, CLAN, start, end)
        for account, samples in (
                # A season reset on 31 August while captured on schedule.
                (steady, ((start, 100), (at(8, 15), 300), (at(8, 31, 12), 20), (at(9, 10), 200))),
                # Never moved: a covered zero.
                (quiet, ((start, 50),)),
                # The counter fell across the outage.
                (gapped, ((start, 80), (at(8, 19, 23), 90), (at(8, 20, 7), 5)))):
            for moment, given in samples:
                await self.counter(account, moment, run=self.legacy, donations=given, donationsReceived=0)
        links = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {"discord_user_id": 41} if tag in ("#PP29L", "#PP29Q") else None for tag in tags})
        context = replace(_context(), account_links=links,
                          bot=SimpleNamespace(activity_reads=self.activity, stay_reads=self.stays))
        window = {"clan_codes": ["BEH"], "after": "2026-08-01T00:00:00+00:00",
                  "before": "2026-10-01T00:00:00+00:00", "by_month": True}
        accounts = await TOOLS[0].handler(context, window)
        self.assertEqual({(row["month"], row["player_tag"]): (row["donations"], row["coverage"])
                          for row in accounts["rows"]}, {
            ("2026-08", "#PP29L"): (220, "C"), ("2026-08", "#PP2L"): (0, "C"), ("2026-08", "#PP29Q"): (15, "P"),
            ("2026-09", "#PP29L"): (180, "C"), ("2026-09", "#PP2L"): (0, "C"), ("2026-09", "#PP29Q"): (0, "C")})
        self.assertEqual(set(accounts["coverage"]), {"C", "P", "N"})
        members = await TOOLS[0].handler(context, {**window, "unit": "member"})
        self.assertCountEqual([(row["month"], row["member_id"], row["donations"], row["coverage"])
                          for row in members["rows"]],
                         [("2026-08", 41, 235, "P"), ("2026-08", None, 0, "C"),
                          ("2026-09", 41, 180, "C"), ("2026-09", None, 0, "C")])
        self.assertTrue(all(row['clan_donations'] is None and row['clan_donations_received'] is None
                            and row['clan_coverage'] == 'N' for row in members['rows']))


if __name__ == "__main__":
    unittest.main()
