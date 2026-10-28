"""Implementation facts and private requester ticket lookup keep their real scopes."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.clans import CLAN_INFO_BOARDS
from elbow_helper.agent.access import can_access_message_channel
from elbow_helper.agent.families.bot_workflows import bot_workflows
from elbow_helper.agent.families.reference_reads import TOOLS
from elbow_helper.agent.families.reference_reads import community_reference
from elbow_helper.agent.knowledge.store import KnowledgeSection
from elbow_helper.agent.models import EvidenceScope
from elbow_helper.agent.families.workflow_reads import requester_tickets
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_HIGH, ASS_PROFILE_LOWER
from agent.engine.test_agent_plan_flow import _context


class BotWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_recruitment_policy_includes_its_implemented_intake_route(self):
        context = _context()
        context.state.scope = EvidenceScope(91, frozenset(), frozenset())
        section = KnowledgeSection(
            'Clan promotion exams: BES to BE4, casual entry and Hellbow assessment',
            'Existing-member promotion policy stays intact.', 'public', 'recruitment-promotions.md')
        catalog = SimpleNamespace(active_sections=(section,), store_status='available',
                                  visible_sections=lambda levels: (section,))
        context = replace(context, knowledge_store=SimpleNamespace(load=lambda: catalog))
        result = await community_reference(context, {'titles': [section.title]})
        self.assertEqual(result['rows'][0]['body'], section.body)
        implementation = result['implementation_reference']
        self.assertEqual(implementation['mode'], 'implementation_reference')
        placement, = implementation['rows']
        self.assertEqual(placement['workflow'], 'recruit_placement')
        self.assertEqual(placement['new_applicant_route'], 'recruitment ticket')
        self.assertFalse(placement['clan_promotion_ticket_required_for_new_applicant'])
        index = await community_reference(context, {})
        self.assertNotIn('implementation_reference', index)
        unrelated = await community_reference(context, {'titles': ['Other']})
        self.assertNotIn('implementation_reference', unrelated)

    async def test_implementation_reference_works_without_a_knowledge_store(self):
        result = await TOOLS[0].handler(_context(), {'reference_kind': 'bot_workflows'})
        self.assertTrue({'ass_scoring', 'ticket_opening', 'recruit_placement',
                         'roster_administration', 'roster_signup_management', 'cwl_thread_cleanup'} <= {r['workflow'] for r in result['rows']})

    async def test_own_ticket_accounts_do_not_require_department_access_or_read_another_owner(self):
        from elbow_helper.agent.families import identity_reads
        context = _context()
        async def history(owner):
            yield SimpleNamespace(author=SimpleNamespace(bot=True), mentions=[SimpleNamespace(id=owner)],
                                  jump_url='https://discord.com/channels/3/101/1000')
        context.guild.text_channels = [SimpleNamespace(id=identifier, name='ticket',
            category_id=RECRUITMENT_TICKET_CATEGORY,
            history=lambda owner=owner, **kw: history(owner),
            permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
            for identifier, owner in ((101, 41), (102, 42))]
        accounts = AsyncMock(return_value={'rows': [{'member_id': 41, 'player_tag': '#P0'}]})
        with patch('elbow_helper.agent.families.workflow_reads.accessible_message_channel',
                   AsyncMock(return_value=True)), patch.object(identity_reads, 'member_accounts', accounts):
            result = await requester_tickets(context, {'member_ids': [41], 'include_accounts': True})
        self.assertEqual([row['ticket_channel_id'] for row in result['rows']], [101])
        self.assertEqual(result['rows'][0]['accounts'], [{'member_id': 41, 'player_tag': '#P0'}])
        accounts.assert_awaited_once_with(context, {'member_ids': [41]})

    def test_cleanup_reference_matches_monthly_guard_and_preserves_protected_messages(self):
        from datetime import datetime, timezone
        from elbow_helper.features.cwl.threads.state import CwlThreadStateMixin
        reference = next(row for row in bot_workflows()['rows'] if row['workflow'] == 'cwl_thread_cleanup')
        self.assertEqual(reference['schedule']['eligible_from_day'], 13)
        self.assertEqual(reference['keeps'], ['First message', 'Configured sticky message', 'Other pinned messages'])
        state = SimpleNamespace(_utc_now=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertIsNone(CwlThreadStateMixin._current_auto_reset_month(state))
        state._utc_now = lambda: datetime(2026, 10, 13, tzinfo=timezone.utc)
        self.assertEqual(CwlThreadStateMixin._current_auto_reset_month(state), '2026-10')

    async def test_automatic_jobs_and_roster_controls_reach_writer_with_live_permission_names(self):
        from elbow_helper.agent.engine.answer_inputs import answer_evidence
        from elbow_helper.configuration.roles import LEAD_PLUS
        context = _context()
        context.guild.roles = [SimpleNamespace(id=next(iter(LEAD_PLUS)), name='Roster manager')]
        result = await TOOLS[0].handler(context, {'reference_kind': 'bot_workflows'})
        visible = answer_evidence({'workflows': result}, guild=context.guild)['workflows']['rows']
        cleanup = next(row for row in visible if row.get('task') == 'cwl thread cleanup')
        self.assertEqual(cleanup['schedule']['eligible_from_day'], 13)
        self.assertIn('Other pinned messages', cleanup['keeps'])
        admin = next(row for row in visible if row.get('entry_point') == '/roster post')
        self.assertEqual(admin['operator_roles'], ['Roster manager'])
        self.assertIn('Remove accounts', admin['remove_signups'])
        self.assertNotIn('sources', str(visible))

    def test_reference_uses_owning_profile_and_recruitment_settings(self):
        rows = {r['workflow']: r for r in bot_workflows()['rows']}
        self.assertEqual([p['difficulty_weight'] for p in rows['ass_scoring']['profiles']],
                         [ASS_PROFILE_HIGH.difficulty_weight, ASS_PROFILE_LOWER.difficulty_weight])
        self.assertEqual(rows['recruit_placement']['supported_clan_codes'], list(CLAN_INFO_BOARDS))
        self.assertIn('BEH', rows['recruit_placement']['supported_clan_codes'])
        self.assertFalse(rows['recruit_placement']['clan_promotion_ticket_required_for_new_applicant'])
        self.assertTrue(rows['ticket_opening']['support']['automatic_welcome'])
        self.assertEqual(rows['ticket_opening']['support']['command'], '/open')

    async def test_lookup_uses_bot_opener_not_names_or_member_prose(self):
        context = replace(_context(), private_pass=True)
        async def history(message):
            yield message
        def ticket(identifier, owner, *, bot=True):
            opener = SimpleNamespace(author=SimpleNamespace(bot=bot), mentions=[SimpleNamespace(id=owner)],
                                     jump_url=f'https://discord.com/channels/3/{identifier}/1000')
            return SimpleNamespace(id=identifier, name='ticket-Test-member',
                category_id=RECRUITMENT_TICKET_CATEGORY,
                history=lambda **kw: history(opener),
                permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        channels = [ticket(101, 41), ticket(102, 42), ticket(103, 41, bot=False)]
        context.guild.text_channels = channels
        with patch('elbow_helper.agent.families.workflow_reads.accessible_message_channel',
                   AsyncMock(side_effect=lambda _, identifier: next(c for c in channels if c.id == identifier))):
            result = await requester_tickets(context, {})
        self.assertEqual([r['ticket_channel_id'] for r in result['rows']], [101])
        self.assertFalse(result['ownership_complete'])
        self.assertEqual(result['unresolved_ticket_count'], 1)
        self.assertEqual(context.state.source_channels, {101})
        denied = await requester_tickets(context, {'member_ids': [42]})
        self.assertEqual(denied['cause'], 'scope_refused')

    async def test_personal_ticket_grant_requires_private_or_shared_destination(self):
        context = _context()
        member = context.member
        channel = SimpleNamespace(id=102, guild=context.guild, type=discord.ChannelType.text,
            category_id=RECRUITMENT_TICKET_CATEGORY,
            overwrites={}, permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        # A real member grant, represented by a hashable Discord-like target.
        target = discord.Object(id=member.id)
        channel.overwrites[target] = discord.PermissionOverwrite(view_channel=True)
        scope = SimpleNamespace(destination_id=91, requester=True, levels=frozenset())
        with patch('elbow_helper.agent.disclosure.request_scope', AsyncMock(return_value=scope)), \
             patch('elbow_helper.agent.disclosure.source_access_levels', return_value=frozenset({'recruitment'})), \
             patch('elbow_helper.agent.access.has_reader_access_requirements', return_value=False), \
             patch('elbow_helper.agent.disclosure.personal_source_allowed', AsyncMock(return_value=False)):
            self.assertTrue(await can_access_message_channel(context, channel))
            scope.requester = False
            self.assertFalse(await can_access_message_channel(context, channel))
            scope.requester = True
            channel.overwrites = {}
            self.assertFalse(await can_access_message_channel(context, channel))
