"""Acceptance-time placement and available cohort evidence stay distinct."""
from dataclasses import replace
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.channels import TRIAL_LIST, TICKETS_LOG
from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage
from elbow_helper.agent.reads import recruitment_history, workflow_reads
from elbow_helper.agent.reads import reference as reference_reads
from elbow_helper.agent.models import EvidenceScope
from elbow_helper.features.recruitment.helpers import HelperMixin
from elbow_helper.features.recruitment.trials import TrialMixin
from agent.engine.test_agent_plan_flow import _context


def role(code):
    return SimpleNamespace(id=CLANS[code].member_role_id, name=code,
        mention=f'<@&{CLANS[code].member_role_id}>', is_default=lambda: False)


def tracking_embed():
    member = SimpleNamespace(id=55, name='Applicant', nick=None, mention='<@55>', roles=[role('BES'), role('BEW')])
    owner = SimpleNamespace(_mention_with_nickname=HelperMixin._mention_with_nickname)
    owner._get_clan_role_mentions = MethodType(HelperMixin._get_clan_role_mentions, owner)
    ticket = SimpleNamespace(mention='<#777>', guild=SimpleNamespace(get_member=lambda _: member))
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    return TrialMixin._build_trial_tracking_embed(owner, ticket, 55, start,
        datetime(2026, 10, 12, tzinfo=timezone.utc)).to_dict()


class RecruitmentHistoryTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = _context()
        context.source_message.created_at = datetime(2026, 10, 24, tzinfo=timezone.utc)
        context.source_message.id = discord.utils.time_snowflake(context.source_message.created_at)
        context = replace(context, bot=SimpleNamespace(user=SimpleNamespace(id=999)),
            message_search=SimpleNamespace(history_page=AsyncMock()))
        context.state.scope = EvidenceScope(91, frozenset({'recruitment'}), frozenset({'recruitment'}))
        return context

    def test_real_tracking_embed_records_start_placement_without_utility_home(self):
        message = {'author_id': 999, 'embeds': [tracking_embed()], 'source': 'synthetic-source'}
        record = recruitment_history.trial_record(message, 999)
        self.assertEqual(record['accepted_clan_codes'], ['BES'])
        # The actual producer uses CLAN_INFO_BOARDS, which excludes BEW.
        self.assertEqual(record['participation_clan_codes'], [])
        self.assertEqual(record['acceptance_decision'], 'accepted_for_trial')
        self.assertIsNone(record['finalized'])
        self.assertEqual(record['ticket_channel_id'], 777)
        self.assertEqual(record['start_at'], '2026-10-05T00:00:00+00:00')
        self.assertIsNone(recruitment_history.trial_record({**message, 'author_id': 55}, 999))
        self.assertIsNone(recruitment_history.trial_record(message, None))
        self.assertIsNone(recruitment_history.trial_record({'author_id': 55,
            'content': 'Accepted for BEW. Declined somewhere else.', 'embeds': []}, 999))
        unknown = tracking_embed()
        next(field for field in unknown['fields'] if field['name'] == 'Clans')['value'] = 'Unknown'
        self.assertIsNone(recruitment_history.trial_record({'author_id': 999,
            'embeds': [unknown]}, 999)['accepted_clan_codes'])

    async def test_tracking_history_uses_real_channel_scope_dates_and_cursors(self):
        context = self.context()
        channel = SimpleNamespace(id=TRIAL_LIST, name='tracking', guild=context.guild)
        async def page(**args):
            message = DiscordSearchMessage(args['before_id'] - 1, args['channel_id'], 999,
                'Bot', '', '2026-10-23T00:00:00+00:00', (tracking_embed(),))
            return DiscordHistoryPage((message,), args['before_id'], args['after_id'], args['limit'],
                args['before_id'] - 2, False)
        context.message_search.history_page.side_effect = page
        arguments = {'stage': 'acceptance_history', 'after': '2026-10-01T00:00:00Z',
                     'before': '2026-10-24T00:00:00Z'}
        with patch('elbow_helper.agent.research.history.accessible_message_channel',
                   AsyncMock(return_value=channel)):
            first = await workflow_reads.recruitment(context, arguments)
            second = await workflow_reads.recruitment(context, {
                **arguments, 'source_cursor': first['source_next_cursor']})
            mismatch = await workflow_reads.recruitment(context, {
                **arguments, 'stage': 'ticket_log', 'source_cursor': first['source_next_cursor']})
        self.assertEqual(first['rows'][0]['accepted_clan_codes'], ['BES'])
        self.assertEqual(second['rows'][0]['accepted_clan_codes'], ['BES'])
        self.assertEqual(context.message_search.history_page.await_count, 2)
        self.assertEqual(context.message_search.history_page.await_args.kwargs['channel_id'], TRIAL_LIST)
        self.assertIn('error', mismatch)
        with patch('elbow_helper.agent.research.history.accessible_message_channel',
                   AsyncMock(return_value=None)):
            denied = await workflow_reads.recruitment(context, arguments)
        self.assertIn('error', denied)
        self.assertNotIn('rows', denied)
        self.assertEqual(context.message_search.history_page.await_count, 2)

    async def test_ticket_transcript_and_member_prose_are_not_classified_as_decisions(self):
        context = self.context()
        message = {'author_id': 55, 'message_id': 7, 'content': 'I was accepted and then declined.',
                   'attachment_names': ['ticket.html'], 'embeds': []}
        reader = AsyncMock(return_value={'messages': [message], 'coverage': {'next_cursor': None}})
        with patch.object(recruitment_history, 'read_discord_channel_history', reader):
            result = await workflow_reads.recruitment(context, {'stage': 'ticket_log',
                'after': '2026-10-01T00:00:00Z', 'before': '2026-10-24T00:00:00Z'})
        row, = result['rows']
        self.assertEqual(row['content'], message['content'])
        self.assertEqual(row['attachment_names'], ['ticket.html'])
        self.assertIsNone(row['acceptance_decision'])
        self.assertEqual(reader.await_args.args[1]['channel_id'], TICKETS_LOG)
        context.state.scope = EvidenceScope(91, frozenset(), frozenset())
        denied = await workflow_reads.recruitment(context, {'stage': 'ticket_log', 'after': '2026-10-01T00:00:00Z'})
        self.assertEqual(denied['cause'], 'scope_refused')
        self.assertEqual(reader.await_count, 1)

    async def test_ticket_log_transcript_can_be_imported_from_authorized_message(self):
        context = self.context()
        data = b'<html><body>Applicant text stays raw. Recruiter: declined.</body></html>'
        attachment = SimpleNamespace(id=88, filename='ticket.html', content_type='text/html',
            size=len(data), read=AsyncMock(return_value=data))
        channel = SimpleNamespace(id=TICKETS_LOG, fetch_message=AsyncMock())
        message = SimpleNamespace(id=77, channel=channel, guild=context.guild,
            attachments=[attachment], jump_url='https://discord.com/channels/3/1135326339092713482/77')
        channel.fetch_message.return_value = message
        arguments = {'mode': 'attachments', 'projection': 'attachment_text',
                     'channel_id': TICKETS_LOG, 'message_id': 77}
        with (patch.object(reference_reads, 'accessible_message_channel', AsyncMock(return_value=channel)),
              patch('elbow_helper.agent.files.attachment_tools.accessible_message_channel',
                    AsyncMock(return_value=channel)),
              patch('elbow_helper.agent.files.attachment_tools.require_evidence_access', AsyncMock())):
            result = await reference_reads.conversation_evidence(context, arguments)
        row, = result['rows']
        self.assertEqual(row['content'], 'Applicant text stays raw. Recruiter: declined.')
        self.assertNotIn('acceptance_decision', row)
        attachment.read.assert_awaited_once()
        channel.fetch_message.assert_awaited_once_with(77)
        self.assertIn(TICKETS_LOG, context.state.source_channels)
        with patch.object(reference_reads, 'accessible_message_channel', AsyncMock(return_value=None)):
            denied = await reference_reads.conversation_evidence(context, arguments)
        self.assertEqual(denied['cause'], 'scope_refused')
        self.assertEqual(channel.fetch_message.await_count, 1)
