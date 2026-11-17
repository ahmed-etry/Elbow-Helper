"""Large participation cohorts remain readable through the real family boundary."""
import json
import unittest

from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.agent.reads.evidence import family_model_view
from elbow_helper.agent.plan.results import model_result, model_view
from tests.agent.engine.test_agent_plan_flow import _context


class ParticipationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_failures_and_role_checks_do_not_make_account_headers_indivisible(self):
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}',
                 'member_id': i + 1000,
                 'profile_status': 'timeout'} for i in range(200)]
        members = [{'member_id': i + 1000, 'membership_status': 'present',
                    'home_role_checks': [{'clan_code': 'example', 'has_role': False}],
                    'roles': [{'role_id': j, 'name': f'Role {j}'} for j in range(25)]}
                   for i in range(200)]
        async def source(context, arguments):
            return {'rows': rows, 'members': members, 'home_role_comparisons': rows,
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
        from elbow_helper.agent.engine.budgets import TOOL_TIMEOUT_SECONDS
        context = replace(_context(), deadline_monotonic=time.monotonic() + 200)
        original_deadline = context.deadline_monotonic
        async def source(read_context, arguments):
            self.assertLessEqual(read_context.deadline_monotonic - time.monotonic(),
                                 TOOL_TIMEOUT_SECONDS - 5)
            return {'rows': [{'player_tag': '#P0', 'attacks_used': 4,
                              'live_status': 'timeout'}]}
        tool = family_tool('read_war_participation', 'Read participation.', {}, source)
        result = await tool.handler(context, {})
        self.assertEqual(result['rows'][0]['attacks_used'], 4)
        self.assertIn(result['report_id'], context.state.reports)
        self.assertEqual(context.deadline_monotonic, original_deadline)


    async def test_a_clan_season_pages_in_short_form_with_every_fact(self):
        # A clan's season: 44 accounts, two per member, each with its attack measurements.
        from elbow_helper.agent.reads.war_measures import aggregate_performance
        from elbow_helper.agent.engine.writer_rows import _ratios
        from elbow_helper.agent.reads.evidence import record_rows
        wars = [f'REG:#CLAN:2026100{day}T180000.000Z:#OPP{day}' for day in range(1, 10)]

        def performance(index):
            measured = {'attacks': 2, 'stars': 4 + index % 2, 'stars_measured': 2, 'triples': 1,
                        'destruction_total': 181.0 + index, 'destruction_measured': 2}
            values = aggregate_performance([{'war_id': war, 'player_tag': f'#P{index}',
                                             'observed_attack_performance': measured} for war in wars])
            return {key: value for key, value in values.items() if key != 'accounts'}
        rows = [{'member_id': 1000 + index // 2, 'player_tag': f'#P{index}', 'player_name': f'Player {index}',
                 'account_tags': [f'#P{index}'], 'war_ids': wars, 'attacks_used': 18 - index % 3,
                 'attacks_expected': 18, 'missed': index % 3, 'miss_rate': index % 3 / 18,
                 'unconfirmed_opportunities': 0, 'unit': 'account', 'missing_link': False,
                 'totals_complete': True, 'participation_status': 'warring',
                 'attack_performance': performance(index), 'member_name': f'Member {index // 2}',
                 'membership_status': 'present', 'account_purpose': None} for index in range(44)]
        members = [{'member_id': 1000 + group, 'member_name': f'Member {group}', 'membership_status': 'present',
                    'account_tags': [f'#P{2 * group}', f'#P{2 * group + 1}']} for group in range(22)]

        async def source(context, arguments):
            return {'rows': rows, 'members': members, 'war_ids': wars, 'clan_participation': []}

        def expanded(table, rendering):
            result = []
            for row in record_rows(table):
                row = {**row, **rendering.get('same_on_every_row', {})}
                if 'account_tags' in rendering.get('omitted_when_repeated', {}):
                    row['account_tags'] = [row['player_tag']]
                if 'attack_performance.wars' in rendering.get('omitted_when_repeated', {}):
                    performance = dict(row['attack_performance'])
                    performance['wars'] = row['wars_observed']
                    performance['stars_measured'] = performance['observed_attacks']
                    row['attack_performance'] = performance
                result.append(row)
            return result
        tool = family_tool('read_war_participation', 'Read participation.', {}, source)
        context, observed = _context(), []
        first = await tool.handler(context, {})
        self.assertEqual(first['returned_rows'], 44)
        arguments = {'report_id': first['report_id'], 'limit': 2}
        while True:
            page = await tool.handler(context, arguments)
            # The step runner views the page, then views what it delivers again.
            view = model_view(model_result(json.loads(json.dumps(model_view(model_result(page))))))
            self.assertNotIn('numerator', json.dumps(view))
            rendering = view['row_rendering']
            shown = expanded(view['rows'], rendering['rows'])
            retained = [_ratios({**{key: value for key, value in row.items() if key != 'war_ids'},
                                 'wars_observed': 9}) for row in page['rows']]
            self.assertEqual([{key: row[key] for key in sorted(row)} for row in shown],
                             [{key: row[key] for key in sorted(row)} for row in retained])
            # Every owner of a row on the page keeps its accounts, even when one member owns them all.
            self.assertEqual([member['member_id'] for member in expanded(view['members'], rendering.get('members', {}))],
                             sorted({row['member_id'] for row in page['rows']}))
            observed.extend(row['player_tag'] for row in page['rows'])
            if not page['next_cursor']:
                break
            arguments = {'report_id': first['report_id'], 'cursor': page['next_cursor'], 'limit': 2}
        self.assertEqual(observed, [row['player_tag'] for row in rows])

    async def test_linked_participation_headers_page_for_both_participation_families(self):
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
                    'war_ids': ['first', 'second'], 'metric': 'participation'}

        for family in ('read_war_participation', 'read_cwl_results'):
            with self.subTest(family=family):
                tool = family_tool(family, 'Read participation.', {}, source)
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
                                 [{'member_id': x['member_id'], 'member_name': x['member_name'],
                                   'account_tags': x['account_tags']}
                                  for x in identities])
                self.assertEqual(json.loads(report.payload)['summary']['clan_participation'], summary)
