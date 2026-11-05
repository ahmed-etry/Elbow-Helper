"""Frozen regular-war facts and joins, using only synthetic collected evidence."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent.engine.test_agent_plan_flow import _context
from agent.families import test_record_wars as fixtures
from elbow_helper.agent.datasets.clash import read_clash
from elbow_helper.agent.families import cohort_activity, projections, record_wars, war_reads
from elbow_helper.agent.families.account_activity import account_activity
from elbow_helper.data.reads import WarReads
from tests.data.test_wars import END, SECOND, WarFixture, war


class WarContracts(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = END + 5 * fixtures.DAY
        for patcher in (
            patch.object(record_wars, 'CLANS', fixtures.FAMILY),
            patch.object(record_wars, 'CODES', {'#PY': 'BEH', '#LQ': 'BE4'}),
            patch.object(war_reads, 'CLANS', fixtures.FAMILY),
            patch('elbow_helper.agent.reads.wars.CLANS', fixtures.FAMILY),
            patch('elbow_helper.data.reads.core.now_us', side_effect=lambda: self.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.forbidden = AsyncMock(side_effect=AssertionError('War facts must not call Clash or the LLM'))
        links = SimpleNamespace(
            get_links_by_tags=lambda tags: {tag: {'discord_user_id': 41} for tag in tags},
            get_links_for_members=lambda ids: {41: [{'player_tag': '#P0'}, {'player_tag': '#P2'}]},
        )
        self.reads = WarReads(self.store)
        self.context = replace(_context(), account_links=links,
            cwl_queries=SimpleNamespace(attendance=lambda **kwargs: {
                'rows': [], 'war_ids': [], 'war_aliases': [], 'wars_without_rosters': [], 'coverage': {}}),
            bot=SimpleNamespace(war_reads=self.reads,
                clash_client=SimpleNamespace(configured=True, get=self.forbidden),
                llm_client=SimpleNamespace(complete=self.forbidden, get_completion=self.forbidden)))

    async def test_record_selection_and_attendance_keep_exact_account_facts(self):
        await self.record(war('warEnded'), at=END + 90 * SECOND)
        selection = {'clan_codes': ['BEH'], **fixtures.OCTOBER}
        source = await war_reads.selected_wars(self.context, selection, 'REG')
        self.assertEqual([(row['player_tag'], row['attacks_used'], row['attacks_expected'], row['missed'])
                          for row in source['rows']], [('#P0', 1, 2, 1), ('#P2', 1, 2, 1)])
        result = await war_reads.attendance(self.context, {
            **selection, 'war_type': 'REG', 'account_tags': ['#P0'], 'unit': 'member'})
        row, = result['rows']
        self.assertEqual((row['account_tags'], row['attacks_expected'], row['missed']), (['#P0'], 2, 1))
        self.assertEqual((row['attack_performance']['wars'], row['attack_performance']['observed_attacks']), (1, 1))
        self.forbidden.assert_not_awaited()

    async def test_account_cohort_and_workflow_measure_joins_keep_selected_accounts(self):
        await self.record(war('warEnded'), at=END + 90 * SECOND)
        with patch('elbow_helper.agent.reads.ranked.require_evidence_access', AsyncMock()):
            rows = [{'player_tag': '#P0'}]
            history = await account_activity(self.context, rows, fixtures.OCTOBER)
        self.assertEqual(history['regular_war']['status'], 'complete')
        self.assertEqual(rows[0]['activity']['regular_war']['attacks_expected'], 2)
        rows = [{'member_id': 41, 'accounts': [{'player_tag': '#P0', 'clan_code': 'BEH'}]}]
        await cohort_activity.activity_projection(self.context, rows, fixtures.OCTOBER)
        self.assertEqual((rows[0]['war_activity']['attacks_expected'], rows[0]['war_activity']['missed']), (2, 1))
        rows = [{'applicant_member_id': 41}]
        result = await projections.performance_projection(self.context, rows,
            {'war_type': 'REG', **fixtures.OCTOBER}, account_tags=['#P0'])
        self.assertNotIn('error', result)
        self.assertEqual(rows[0]['attack_performance']['REG']['accounts'], ['#P0'])
        self.assertEqual(rows[0]['war_attendance']['attacks_expected'], 2)
        self.forbidden.assert_not_awaited()

    async def test_current_war_checks_keep_unused_attacks_and_unavailable_states(self):
        await self.record(war('inWar'), at=self.now - 10 * SECOND)
        tool = fixtures.TOOLS['read_current_war']
        result = await tool.handler(self.context, {'clan_codes': ['BEH'], 'account_tags': ['#P0']})
        row, = result['rows'][0]['members']
        self.assertEqual((row['player_tag'], row['attacks_used'], row['unused_attacks'], row['missed']),
                         ('#P0', 1, 1, None))
        for payload, status, expected in ((None, 503, 'unavailable'), ({'state': 'notInWar'}, 200, 'ok')):
            self.now += 30 * SECOND
            await self.record(payload, status=status, at=self.now - 10 * SECOND)
            result = await war_reads.current_war(self.context, {'clan_codes': ['BEH']})
            row, = result['rows']
            self.assertEqual(row['status'], expected)
            self.assertEqual(row['state'], 'notInWar' if expected == 'ok' else None)
        self.now += fixtures.DAY
        row, = (await war_reads.current_war(self.context, {'clan_codes': ['BEH']}))['rows']
        self.assertEqual((row['status'], row['state']), ('stale', None))
        self.forbidden.assert_not_awaited()

    async def test_failed_measure_read_is_unavailable_in_each_join(self):
        with patch.object(self.reads, 'regular_selection', AsyncMock(side_effect=OSError('offline'))):
            rows = [{'player_tag': '#P0'}]
            await account_activity(self.context, rows, fixtures.OCTOBER)
            self.assertEqual(rows[0]['activity']['regular_war']['status'], 'failed')
            rows = [{'member_id': 41, 'accounts': [{'player_tag': '#P0', 'clan_code': 'BEH'}]}]
            await cohort_activity.activity_projection(self.context, rows, fixtures.OCTOBER)
            self.assertEqual(rows[0]['war_activity']['status'], 'source_unavailable')
            result = await projections.performance_projection(self.context, [{'member_id': 41}],
                {'war_type': 'REG', **fixtures.OCTOBER})
            self.assertEqual(result['cause'], 'source_unavailable')
        self.forbidden.assert_not_awaited()

    async def test_current_misses_require_final_counts_even_after_the_war_ends(self):
        await self.record(war('warEnded'), at=self.now - 10 * SECOND)
        await self.execute("UPDATE war_sides SET attacks=4 WHERE clan_tag='#PY'")
        result = await war_reads.current_war(self.context, {'clan_codes': ['BEH']})
        self.assertEqual([row['unused_attacks'] for row in result['rows'][0]['members']], [1, 1])
        self.assertEqual([row['missed'] for row in result['rows'][0]['members']], [None, None])
        self.forbidden.assert_not_awaited()

    async def test_one_physical_war_has_one_identity_in_member_measurements(self):
        await self.record(war('warEnded', clans=('#PY', '#LQ')), at=END + 90 * SECOND)
        result = await war_reads.attendance(self.context, {
            'war_type': 'REG', 'unit': 'member', 'clan_codes': ['BEH', 'BE4'], 'latest_completed': 1})
        self.assertEqual(len(result['war_ids']), 1)
        row, = result['rows']
        self.assertEqual(len(row['war_ids']), 1)
        self.assertEqual((row['attack_performance']['wars'], row['attack_performance']['account_war_opportunities']),
                         (1, 4))
        self.assertEqual({total['clan_code']: total['wars'] for total in result['clan_totals']}, {'BEH': 1, 'BE4': 1})
        self.forbidden.assert_not_awaited()


class ExternalWarContracts(unittest.IsolatedAsyncioTestCase):
    async def test_family_clans_have_no_raw_war_fact_path_even_without_a_client(self):
        from elbow_helper.agent.datasets.clash import CLANS, clash_tools
        client = SimpleNamespace(configured=True, get=AsyncMock(side_effect=AssertionError('No live family war read')))
        context = SimpleNamespace(bot=SimpleNamespace(clash_client=client), deadline_monotonic=None)
        code = next(iter(CLANS))
        with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()):
            for kind in ('current_war', 'war_log'):
                for selection in ({}, {'detail': 'full'}, {'fields': ['clan.members.attacks']}):
                    result = await read_clash(context, {'kind': kind, 'tags': [code], **selection})
                    item, = result['items']
                    self.assertEqual(item['status'], 'record_required')
                    self.assertNotIn('data', item)
                result = await read_clash(context, {'kind': kind, 'tags': [code, '#P0']})
                self.assertEqual([item['status'] for item in result['items']], ['record_required', 'unavailable'])
                client.get.reset_mock()
                context.bot.clash_client = None
                result = await read_clash(context, {'kind': kind, 'tags': [CLANS[code].tag]})
                self.assertEqual(result['items'][0]['status'], 'record_required')
                context.bot.clash_client = client
                client.get.assert_not_awaited()
        note = clash_tools()[0].definition.usage_note
        self.assertIn('current_war/war_log: uncollected clans only', note)
        self.assertLessEqual(len(note), 160)

    async def test_raw_current_war_and_log_keep_external_payloads_and_failures(self):
        client = SimpleNamespace(configured=True, get=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(clash_client=client), deadline_monotonic=None)
        with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()):
            for kind, payload in (('current_war', war()), ('war_log', {'items': [{'result': 'win'}]})):
                client.get.return_value = SimpleNamespace(ok=True, status=200, payload=payload)
                result = await read_clash(context, {'kind': kind, 'tags': ['#P0'], 'detail': 'full'})
                self.assertEqual(result['items'][0]['data'], payload)
                self.assertEqual(result['source'], 'clash_api')
                for status, expected in ((403, 'private'), (503, 'maintenance'), (404, 'not_found')):
                    client.get.return_value = SimpleNamespace(ok=False, status=status, payload=None)
                    result = await read_clash(context, {'kind': kind, 'tags': ['#P0']})
                    self.assertEqual(result['items'][0]['status'], expected)


if __name__ == '__main__':
    unittest.main()
