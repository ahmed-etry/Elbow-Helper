"""A saved workflow report larger than its pages reaches the answer step whole."""
from dataclasses import replace
from itertools import islice, product
from types import SimpleNamespace
import unittest

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.plan.results import model_result
from elbow_helper.agent.reads.donations import TOOLS as DONATION_TOOLS
from elbow_helper.agent.reads.evidence import record_rows
from elbow_helper.agent.reads.workflow_reads import TOOLS
from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.hibernation.queries import HibernationQueries
from tests.agent.engine.test_agent_plan_flow import _context


class WholeReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_hibernation_report_beyond_two_pages_reaches_the_answer_step_whole(self):
        home = SimpleNamespace(id=CLANS['BEM'].member_role_id, name='BEM', is_default=lambda: False)
        context = _context()
        for role in context.member.roles:
            role.name = 'Access'
            role.is_default = lambda: False
        log = SimpleNamespace(**{**vars(context.source_message.channel), 'id': HIBERNATION_LOG})
        context.guild.get_channel_or_thread = lambda identifier: (
            log if identifier == HIBERNATION_LOG else context.source_message.channel)
        actor = context.member
        context.guild.get_member = lambda identifier: actor if identifier == actor.id else SimpleNamespace(
            id=identifier, display_name='Member %d' % identifier, nick=None, roles=[home], joined_at=None)
        state = {str(1000 + number): {'hibernation_date': '<t:%d:F>' % (1_700_000_000 + number * 86400)}
                 for number in range(700)}
        context = replace(context, hibernation_queries=HibernationQueries(lambda: state))
        tool = next(tool for tool in TOOLS if tool.definition.name == 'read_hibernation')
        page = await tool.handler(context, {'sort': 'longest_hibernating'})
        self.assertEqual(page['total_rows'], 700)
        # Two pages of this size still leave members unread.
        self.assertLess(2 * len(page['rows']), 700)
        written = await cohort_writer_evidence(context, {'hibernating': model_result(page)})
        facts = answer_evidence(written, guild=context.guild)['hibernating']
        self.assertEqual(len(record_rows(facts['rows'])), 700)
        self.assertNotIn('evidence_complete', facts)
        self.assertNotIn('row_coverage', facts)

    async def test_donation_report_beyond_one_page_reaches_the_answer_step_whole(self):
        tags = ['#' + ''.join(letters) for letters in islice(product('0289PYLQGRJCUV', repeat=3), 700)]

        async def donation_windows(chunk, windows):
            return {'rows': [{'player_tag': tag, 'after': after, 'before': before, 'donations': 5,
                              'donations_received': 3, 'coverage': 'C'}
                             for tag in chunk for after, before in windows]}

        async def accounts(chunk):
            return {'rows': [{'player_tag': tag, 'player_name': 'Account ' + tag[2:]} for tag in chunk]}

        links = SimpleNamespace(get_links_by_tags=lambda requested: dict.fromkeys(requested))
        context = replace(_context(), account_links=links, bot=SimpleNamespace(
            activity_reads=SimpleNamespace(donation_windows=donation_windows),
            stay_reads=SimpleNamespace(accounts=accounts)))
        tool = next(tool for tool in DONATION_TOOLS if tool.definition.name == 'read_donation_history')
        page = await tool.handler(context, {'account_tags': tags, 'after': '2026-08-01T00:00:00+00:00',
                                            'before': '2026-09-01T00:00:00+00:00'})
        self.assertEqual(page['total_rows'], 700)
        # Two pages of this size still leave accounts unread.
        self.assertLess(2 * len(page['rows']), 700)
        written = await cohort_writer_evidence(context, {'donations': model_result(page)})
        facts = answer_evidence(written, guild=context.guild)['donations']
        self.assertEqual(len(record_rows(facts['rows'])), 700)
