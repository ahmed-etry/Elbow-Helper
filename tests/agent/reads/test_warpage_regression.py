"""The named-member war read that exceeded its planner page still reaches the writer."""
from copy import deepcopy
from dataclasses import replace
from itertools import islice, product
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.reads.evidence import record_rows
from elbow_helper.agent.reads import record_wars, war_span, wars
from elbow_helper.data.reads import CwlReads, StayReads, WarReads
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory
from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_family_wars import TOOLS, OCTOBER
from tests.agent.reads.test_record_wars import FAMILY
from tests.data.test_wars import END, SECOND, WarFixture, war


class NamedWarPageTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for patcher in (patch.object(record_wars, 'CLANS', FAMILY),
                        patch.object(record_wars, 'CODES', {'#PY': 'BEH', '#LQ': 'BE4'}),
                        patch.object(wars, 'CLANS', FAMILY), patch.object(war_span, 'CLANS', FAMILY),
                        patch('elbow_helper.data.reads.core.now_us', return_value=END + 6 * 86400 * SECOND)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.context = replace(_context(), bot=SimpleNamespace(war_reads=WarReads(self.store),
            stay_reads=StayReads(self.store)), cwl_queries=CwlQueries(CwlHistory(CwlReads(self.store), None, FAMILY), lambda: {}))

    async def test_twelve_members_with_many_idle_accounts_keep_rows_and_every_members_figures(self):
        tags = ['#' + ''.join(parts) for parts in islice(product('0289PYLQGRJCUV', repeat=5), 192)]
        members = list(range(100, 112))
        links = {member: [{'player_tag': tag} for tag in tags[index * 16:(index + 1) * 16]]
                 for index, member in enumerate(members)}
        owners = {link['player_tag']: member for member, accounts in links.items() for link in accounts}
        # One active account and fifteen idle accounts per member, as in the live failure.
        payload = war('warEnded', end='20261101T180000.000Z')
        payload.update(teamSize=12, preparationStartTime='20261030T180000.000Z',
                       startTime='20261031T180000.000Z')
        for side in ('clan', 'opponent'):
            template = payload[side]['members'][0]
            payload[side]['members'] = []
            for index, member in enumerate(members):
                row = deepcopy(template)
                row.update(tag=links[member][0]['player_tag'] if side == 'clan' else '#C' + tags[index][1:],
                           mapPosition=index + 1)
                row.pop('bestOpponentAttack', None)
                row['attacks'] = []
                payload[side]['members'].append(row)
            payload[side].update(attacks=0, stars=0, destructionPercentage=0)
        await self.record(payload, at=END + 5 * 86400 * SECOND)
        context = replace(self.context, account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {member: links[member] for member in ids},
            get_links_by_tags=lambda selected: {tag: {'discord_user_id': owners[tag]}
                                               for tag in selected if tag in owners}))
        tool = TOOLS['read_war_participation']
        arguments = {'war_type': 'both', 'unit': 'member', 'member_ids': members,
                     **OCTOBER, 'before': '2026-11-03T00:00:00+00:00'}
        runner = PlanRunner(service=AgentService(None), context=context,
            registry={tool.definition.name: tool}, command_capabilities={},
            budget=ContextBudget(None, 8192, 0), rounder=SimpleNamespace(social_route=False))
        results = await runner.run({'steps': [{'id': 'wars', 'capability': tool.definition.name,
                                              'arguments': arguments, 'depends_on': []}]})
        page = results['wars']
        self.assertNotIn('error', page)
        self.assertGreater(page['returned_rows'], 0)
        self.assertEqual(page['total_rows'], 12)
        report = context.state.reports[page['report_id']]
        saved = json.loads(report.payload)
        self.assertNotIn('accounts', saved['summary']['record_span'])
        self.assertNotIn('members', saved['summary'])
        self.assertTrue(all('identities' not in group for clan in saved['summary']['clan_participation']
                            for group in clan.values() if isinstance(group, dict)))
        for row in saved['rows']:
            idle = [entry for entry in row['family_wars'] if entry.get('wars') == 0]
            self.assertEqual(len(idle), 15)
            self.assertTrue(all(not any(key in entry for key in ('regular', 'cwl', 'combined', 'by_clan'))
                                for entry in idle))
        written = await cohort_writer_evidence(context, runner.model_results_for(results),
                                               snapshots=runner.report_snapshots)
        facts = answer_evidence(written, guild=context.guild)['wars']
        rendering = facts.get('row_rendering', {}).get('rows', {}).get('same_on_every_row', {})
        rows = [{**rendering, **row} for row in record_rows(facts['rows'])]
        self.assertEqual({row['member_id']: (row['attacks_used'], row['attacks_expected'], row['missed'])
                          for row in rows}, {member: (0, 2, 2) for member in members})
