"""Large attendance cohorts remain readable through the real family boundary."""
import json
import unittest

from elbow_helper.features.agent.families.core import family_tool
from elbow_helper.features.agent.families.model_view import family_model_view
from elbow_helper.features.agent.plan.results import model_result, model_view
from features.agent.engine.test_agent_plan_flow import _context


class AttendanceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_failures_and_role_checks_do_not_make_account_headers_indivisible(self):
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}',
                 'member_id': i + 1000, 'membership_status': 'present',
                 'home_role_checks': [{'clan_code': 'example', 'has_role': False}],
                 'roles': [{'role_id': j, 'name': f'Role {j}'} for j in range(25)],
                 'profile_status': 'timeout'} for i in range(200)]
        async def source(context, arguments):
            return {'rows': rows, 'home_role_comparisons': rows,
                    'unknown_accounts': rows, 'unknown_profile_count': 200,
                    'count_complete': False}
        tool = family_tool('read_member_accounts', 'Read accounts.', {}, source)
        context, observed, arguments = _context(), [], {}
        while True:
            page = await tool.handler(context, arguments)
            self.assertNotIn('error', page)
            view = model_view(model_result(page))
            selected = view['rows']
            selected = [dict(zip(selected['columns'], row)) for row in selected['rows']] \
                       if isinstance(selected, dict) else selected
            observed.extend(row['player_tag'] for row in selected)
            self.assertEqual(view['unknown_profile_count'], 200)
            self.assertEqual(view['flags']['status'], 'partial')
            self.assertLessEqual(len(json.dumps(view, ensure_ascii=False,
                                                separators=(',', ':'))), 16000)
            if not page['next_cursor']:
                break
            arguments = {'report_id': page['report_id'], 'cursor': page['next_cursor']}
        self.assertEqual(observed, [row['player_tag'] for row in rows])

    async def test_slow_live_reads_have_time_to_return_and_retain_partial_observations(self):
        import time
        from dataclasses import replace
        from elbow_helper.features.agent.engine.budgets import TOOL_TIMEOUT_SECONDS
        context = replace(_context(), deadline_monotonic=time.monotonic() + 200)
        original_deadline = context.deadline_monotonic
        async def source(read_context, arguments):
            self.assertLessEqual(read_context.deadline_monotonic - time.monotonic(),
                                 TOOL_TIMEOUT_SECONDS - 5)
            return {'rows': [{'player_tag': '#P0', 'attacks_used': 4,
                              'live_status': 'timeout'}]}
        tool = family_tool('read_war_attendance', 'Read attendance.', {}, source)
        result = await tool.handler(context, {})
        self.assertEqual(result['rows'][0]['attacks_used'], 4)
        self.assertIn(result['report_id'], context.state.reports)
        self.assertEqual(context.deadline_monotonic, original_deadline)

    async def test_repeated_clan_identities_do_not_block_complete_account_pages(self):
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}',
                 'member_id': None, 'attacks_used': i % 8, 'attacks_expected': 8,
                 'missed': 8 - i % 8, 'totals_complete': True} for i in range(240)]
        identities = [{'member_id': None, 'member_name': None,
                       'player_tag': r['player_tag'], 'player_name': r['player_name'],
                       'account_tags': [r['player_tag']], 'unit': 'account'} for r in rows]
        summary = [{'clan_code': code, 'warring': {'member_count': 0,
                    'unlinked_account_count': len(identities), 'identities': identities}}
                   for code in ('first', 'second', 'third', 'fourth')]

        async def source(context, arguments):
            return {'rows': rows, 'clan_participation': summary}

        tool = family_tool('read_war_attendance', 'Read attendance.', {}, source)
        context, observed, arguments = _context(), [], {}
        while True:
            page = await tool.handler(context, arguments)
            self.assertNotIn('error', page)
            view = model_view(model_result(page))
            # The runner projects a result again after paging/compacting its
            # summary. That second boundary must accept the same identities.
            repeated = family_model_view({**view, 'rows': page['rows']})
            self.assertEqual(repeated['clan_participation'],
                             family_model_view(page)['clan_participation'])
            self.assertLessEqual(len(json.dumps(view, ensure_ascii=False,
                                                separators=(',', ':'))), 16000)
            table = view['rows']
            page_rows = [dict(zip(table['columns'], row)) for row in table['rows']]
            observed.extend(page_rows)
            for clan in view['clan_participation']:
                selected = clan['warring']['identities']
                self.assertEqual(selected['rows'], [[r['player_tag']] for r in page_rows])
                self.assertEqual(clan['warring']['unlinked_account_count'], 240)
            if not page['next_cursor']:
                break
            arguments = {'report_id': page['report_id'], 'cursor': page['next_cursor']}
        self.assertEqual(observed, rows)
        retained = json.loads(context.state.reports[page['report_id']].payload)
        self.assertEqual(retained['summary']['clan_participation'], summary)

    async def test_linked_participation_headers_page_for_both_attendance_families(self):
        # One owner has many accounts. Identity summaries must follow account
        # pages without pulling every other alt into an indivisible header.
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}',
                 'member_id': 1000 + i // 100, 'unit': 'account',
                 'account_tags': [f'#P{i}'], 'war_ids': ['first', 'second'],
                 'attacks_used': 4, 'attacks_expected': 4, 'missed': 0}
                for i in range(700)]
        identities = [{'member_id': 1000 + group, 'member_name': f'Member {group}',
                       'account_tags': [f'#P{i}' for i in range(group * 100, (group + 1) * 100)]}
                      for group in range(7)]
        summary = [{'clan_code': code, 'warring': {'member_count': 7,
                    'unlinked_account_count': 0, 'identities': identities}}
                   for code in ('first', 'second', 'third', 'fourth')]

        async def source(context, arguments):
            return {'rows': rows, 'clan_participation': summary,
                    'war_ids': ['first', 'second'], 'metric': 'attendance'}

        for family in ('read_war_attendance', 'read_cwl_results'):
            with self.subTest(family=family):
                tool = family_tool(family, 'Read attendance.', {}, source)
                context, observed, arguments, pages = _context(), [], {}, 0
                while True:
                    page = await tool.handler(context, arguments)
                    self.assertNotIn('error', page)
                    view = model_view(model_result(page))
                    selected = {row['player_tag'] for row in page['rows']}
                    observed.extend(row['player_tag'] for row in page['rows'])
                    self.assertEqual(view['participation_scope']['counts'], 'full selected report')
                    self.assertLessEqual(len(json.dumps(view, ensure_ascii=False,
                                                        separators=(',', ':'))), 16000)
                    for clan in family_model_view(page)['clan_participation']:
                        self.assertEqual(clan['warring']['member_count'], 7)
                        self.assertEqual({tag for identity in clan['warring']['identities']
                                          for tag in identity['account_tags']}, selected)
                    pages += 1
                    if not page['next_cursor']:
                        break
                    arguments = {'report_id': page['report_id'], 'cursor': page['next_cursor']}
                self.assertGreater(pages, 1)
                self.assertEqual(observed, [row['player_tag'] for row in rows])
                report = context.state.reports[page['report_id']]
                full = family_model_view(report.page(limit=len(rows)))
                self.assertEqual(full['clan_participation'][0]['warring']['identities'],
                                 [{'member_id': x['member_id'], 'account_tags': x['account_tags']}
                                  for x in identities])
                self.assertEqual(json.loads(report.payload)['summary']['clan_participation'], summary)
