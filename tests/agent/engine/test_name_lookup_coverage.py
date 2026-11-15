"""Unread account pages and unrequested measures remain unknown to the writer."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.budgets import FAMILY_LIMITS
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.agent.plan.results import compact_result, model_result
from tests.agent.engine.test_agent_plan_flow import _context, _plan
from tests.agent.engine.test_answer_composition import call, client, provider_response, writer_payload
from tests.agent.engine.test_writer_delivery_evidence import unpack
from tests.agent.reads.test_name_resolution_view import named_accounts


class NameLookupCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_first_middle_and_last_pages_expose_unread_accounts_without_flags(self):
        for offset, cursor in ((0, 'opaque'), (1, 'opaque'), (2, None)):
            payload = named_accounts(2)
            payload.update(rows=payload['rows'][offset:offset + 1], total_rows=4,
                           returned_rows=1, offset=offset, next_cursor=cursor)
            before = deepcopy(payload)
            for compact in (False, True):
                result = answer_evidence({'accounts': compact_result(payload) if compact else payload})['accounts']
                self.assertFalse(result['evidence_complete'])
                self.assertEqual(result['row_coverage'], {'status': 'partial', 'rows_read': 1,
                    'rows_not_read': 3, 'account_tags_not_read': sorted(
                        {'#P00', '#P01', '#P10', '#P11'} - {payload['rows'][0]['player_tag']})})
                self.assertNotIn('next_cursor', result)
            self.assertEqual(payload, before)

    def test_cursor_without_count_still_marks_unknown_rows(self):
        result = answer_evidence({'read': {'family': 'read_test_family', 'rows': [],
            'next_cursor': 'opaque', 'selection': {'account_tags': ['#P0']}}})['read']
        self.assertFalse(result['evidence_complete'])
        self.assertEqual(result['row_coverage'], {'status': 'not_read', 'rows_read': 0,
            'rows_not_read': None, 'account_tags_not_read': ['#P0']})


    def test_count_only_view_does_not_become_an_incomplete_row_read(self):
        result = answer_evidence({'read': {'family': 'read_test_family', 'rows': [],
            'count': 20, 'total_rows': 20, 'returned_rows': 0, 'next_cursor': None}})['read']
        self.assertEqual(result['count'], 20)
        self.assertNotIn('row_coverage', result)
        self.assertNotIn('evidence_complete', result)

    def test_unresolved_step_references_are_not_account_tags(self):
        result = answer_evidence({'read': {'family': 'read_war_participation', 'rows': [],
            'next_cursor': 'opaque'}}, subjects={'read': {
                'account_tags': {'from_step': 'accounts', 'path': 'rows.player_tag'}}})['read']
        self.assertEqual(result['row_coverage']['account_tags_not_read'], [])

    def test_failed_name_read_does_not_supply_subjects_to_measure_coverage(self):
        accounts = named_accounts(1)
        accounts['flags'] = {'status': 'failed'}
        result = answer_evidence({'accounts': accounts, 'wars': {
            'family': 'read_war_participation', 'rows': []}})
        self.assertNotIn('row_coverage', result['wars'])

    def test_checked_empty_measure_is_distinct_from_unread_named_accounts(self):
        accounts = named_accounts(1)
        empty = {'family': 'read_cwl_results', 'rows': [], 'total_rows': 0,
                 'selection': {'account_tags': ['#P00']}, 'unobserved_accounts': ['#P00']}
        result = answer_evidence({'accounts': accounts, 'cwl': empty})['cwl']
        self.assertEqual(result['unobserved_accounts'], ['#P00'])
        self.assertEqual(result['row_coverage']['account_tags_not_read'], ['#P01'])
        complete = answer_evidence({'cwl': empty})['cwl']
        self.assertNotIn('row_coverage', complete)

    async def test_authorized_restoration_clears_unread_identity_pages(self):
        context = _context()
        payload = named_accounts()
        tool = family_tool('read_member_accounts', 'Synthetic accounts.', {}, AsyncMock(return_value=payload))
        page = model_result(await tool.handler(context, {'limit': 1}))
        self.assertEqual(len(answer_evidence({'accounts': page})['accounts']
                             ['row_coverage']['account_tags_not_read']), 13)
        rest = model_result(await tool.handler(context, {'report_id': page['report_id'],
                                                         'cursor': page['next_cursor']}))
        restored = await cohort_writer_evidence(context, {'accounts': page, 'rest': rest})
        written = answer_evidence(restored)['accounts']
        self.assertNotIn('row_coverage', written)
        self.assertNotIn('evidence_complete', written)

    async def test_an_answer_forced_after_a_refused_plan_is_told_the_unshown_rows_were_read(self):
        # Live 15 Nov: 133 rows read and saved, one page shown, a plan for the rest refused by the
        # plan check, and no research round left for its correction.
        rows = [{'player_tag': '#P%03d' % index, 'stars': index % 4, 'note': 'n' * 150} for index in range(133)]
        tool = family_tool('read_test_family', 'Synthetic saved rows.', {}, AsyncMock(return_value={'rows': rows}))
        registry = {'read_test_family': tool}
        requests = []

        async def provider(**options):
            requests.append(options)
            if options['messages'][0]['content'] == ROUTE_INSTRUCTION:
                return provider_response('research')
            if len(requests) == 2:
                return provider_response(calls=[call('submit_request_plan', {**_plan([
                    {'id': 'rows', 'capability': 'read_test_family', 'arguments': {}, 'depends_on': []}],
                    effort='high'), 'finish_after_reads': True})])
            if len(requests) == 3:
                handle =json.loads(options['messages'][1]['content'].split('\nAvailable evidence:\n')[0])
                cursor = handle['read_handles']['rows']
                return provider_response(calls=[call('submit_request_plan', {**_plan([
                    {'id': 'rest', 'capability': 'read_test_family', 'arguments': {
                        'report_id': cursor['report_id'], 'cursor': cursor['next_cursor']}, 'depends_on': []}],
                    effort='medium'), 'finish_after_reads': True})])
            return provider_response('Synthetic answer.')

        model = client(provider)
        try:
            with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry), \
                    patch('elbow_helper.agent.engine.budgets.RESEARCH_LIMITS', FAMILY_LIMITS):
                answer = await AgentService(model).answer(question='Synthetic star pace.',
                                                          local_context='', context=_context())
        finally:
            await model.close()
        self.assertEqual(answer, 'Synthetic answer.')
        self.assertEqual(len(requests), 4)
        forced = requests[3]
        self.assertIn('No further research is available', json.dumps(forced['messages'], default=str))
        _, _, evidence = writer_payload(forced)
        coverage = unpack(evidence)['rows']['row_coverage']
        self.assertEqual(coverage['rows_read'], 133)
        self.assertLess(coverage['rows_shown'], 133)
        self.assertNotIn('rows_not_read', coverage)

    async def test_writer_input_keeps_all_named_accounts_and_marks_unread_wars(self):
        payload = named_accounts()
        accounts = family_tool('read_member_accounts', 'Synthetic accounts.', {},
                               AsyncMock(return_value=payload))
        wars = family_tool('read_war_participation', 'Synthetic wars.', {
            'account_tags': {'type': 'array', 'items': {'type': 'string'}}}, AsyncMock(return_value={
                'rows': [{'player_tag': '#P00', 'attacks_used': 3, 'attacks_expected': 4}]}))
        registry = {tool.definition.name: tool for tool in (accounts, wars)}
        final = 'Synthetic account activity; other accounts have not been checked.'
        requests = []

        async def provider(**options):
            requests.append(options)
            system = options['messages'][0]['content']
            if system == ROUTE_INSTRUCTION:
                return provider_response('research')
            if system == answer_instructions(registry):
                _, _, evidence = writer_payload(options)
                evidence = unpack(evidence)
                self.assertEqual(len(evidence['accounts']['rows']), 14)
                self.assertNotIn('row_coverage', evidence['accounts'])
                self.assertEqual(evidence['wars']['row_coverage']['account_tags_not_read'],
                    sorted(row['player_tag'] for row in payload['rows'] if row['player_tag'] != '#P00'))
                self.assertIn('Their records are unknown, never absent.', system)
                self.assertNotIn('next_cursor', evidence['accounts'])
                return provider_response(final)
            return provider_response(calls=[call('submit_request_plan', _plan([
                {'id': 'accounts', 'capability': 'read_member_accounts',
                 'arguments': {}, 'depends_on': []},
                {'id': 'wars', 'capability': 'read_war_participation',
                 'arguments': {'account_tags': ['#P00']}, 'depends_on': ['accounts']}]))])

        model = client(provider)
        try:
            with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                answer = await AgentService(model).answer(question='Compare synthetic account activity.',
                    local_context='', context=_context())
            self.assertEqual(answer, final)
            self.assertEqual(len(requests), 3)
        finally:
            await model.close()


if __name__ == '__main__':
    unittest.main()
