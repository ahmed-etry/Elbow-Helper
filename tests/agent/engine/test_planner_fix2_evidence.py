"""Complete typed comparisons and tab summaries survive the real writer boundary."""

from dataclasses import dataclass, replace
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from openpyxl import load_workbook

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.models import AgentDelivery
from elbow_helper.agent.families import identity_reads, war_reads
from elbow_helper.agent.families.access_reads import TOOLS as ACCESS_TOOLS
from elbow_helper.agent.families.core import family_tool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan
from agent.engine.test_writer_tables import expand as unpack


class WriterModel(_Model):
    def __init__(self):
        super().__init__(_Session([], []))
        self.writer = None

    def create_agent_session(self, **kwargs):
        if not kwargs['tools']:
            self.writer = kwargs
            return _Session([], [])
        return super().create_agent_session(**kwargs)


class PlannerFix2EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def writer(self, context, registry, steps):
        model = WriterModel()
        # Enable the production family catalogue and its ordinary request caps.
        first = next(iter(registry.values()))
        registry = {**registry, 'hidden': replace(first, default_visible=False,
            definition=AgentToolDefinition('hidden', 'Hidden specialist.', {}))}
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(model)._start_flow(question='Compare the selected facts.',
                local_context='', context=context, conversation_history='')
        results = await flow.runner.run(_plan(steps))
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        self.assertTrue(await flow._prepare_answer_session())
        self.last_writer = model.writer
        evidence = unpack(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1]))
        self.assertFalse(evidence.get('truncated'))
        return flow, evidence['results']

    async def test_roster_checks_cover_every_account_with_unknown_owners_and_failed_profiles(self):
        context = _context()
        home_id = CLANS['BES'].member_role_id
        members = {}
        for identifier in range(100, 143):
            roles = [SimpleNamespace(id=i + 1000, name='Cosmetic ' + str(i), is_default=lambda: False)
                     for i in range(80)]
            if identifier != 142:
                roles.append(SimpleNamespace(id=home_id, name='BES', is_default=lambda: False))
            members[identifier] = SimpleNamespace(id=identifier, display_name='Member ' + str(identifier),
                roles=roles, nick=None, joined_at=None)
        original = context.guild.get_member
        context.guild.get_member = lambda key: members.get(key) or (original(key) if key in (41, 999) else None)
        alphabet = '0289PYLQGRJCUV'
        tags = ['#P' + alphabet[i // len(alphabet)] + alphabet[i % len(alphabet)] for i in range(47)]
        links = {tag: {'discord_user_id': 100 + i, 'player_tag': tag} if i < 44 else None
                 for i, tag in enumerate(tags)}
        context = replace(context, account_links=SimpleNamespace(get_links_by_tags=lambda values: links,
            get_links_for_members=lambda identifiers: {identifier: [link for link in links.values()
                if link and link['discord_user_id'] == identifier] for identifier in identifiers}))
        roster = family_tool('read_clan_rosters', 'Read selected roster.', {}, AsyncMock(return_value={
            'rows': [{'player_tag': tag, 'clan_code': 'BES', 'player_name': 'Roster account ' + str(i)}
                     for i, tag in enumerate(tags)]}))
        accounts = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_member_accounts')
        profiles = {tag: {'status': 'ok', 'data': {'name': 'Account ' + str(i)}} for i, tag in enumerate(tags)}
        for tag in tags[1:18]:
            profiles[tag] = {'status': 'unavailable'}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            flow, evidence = await self.writer(context, {'read_clan_rosters': roster,
                'read_member_accounts': accounts}, [
                {'id': 'roster', 'capability': 'read_clan_rosters', 'arguments': {}, 'depends_on': []},
                {'id': 'accounts', 'capability': 'read_member_accounts', 'arguments': {
                    'cohort_ref': {'step': 'roster', 'path': ['report_id']}, 'include_roles': True},
                 'depends_on': ['roster']}])
        rows = {r['player_tag']: r for r in evidence['accounts']['home_role_comparisons']}
        self.assertEqual(set(rows), set(tags))
        self.assertEqual(rows[tags[1]]['player_name'], 'Roster account 1')
        self.assertTrue(evidence['accounts']['role_comparison_complete'])
        self.assertEqual(len(evidence['accounts']['unknown_accounts']), 17)
        self.assertTrue(all('roles' not in r for r in evidence['accounts']['unknown_accounts']))
        self.assertEqual(len(evidence['accounts']['rows']), len(tags))
        self.assertTrue(evidence['accounts']['members'])
        self.assertTrue(all('roles' in r for r in evidence['accounts']['members']))
        self.assertEqual([role['role_id'] for role in evidence['accounts']['members'][0]['roles']], [home_id])
        self.assertTrue(all(not r.get('roles') for r in evidence['accounts']['rows']))
        profiles = {row['member_id']: row for row in evidence['accounts']['members']}
        for i, tag in enumerate(tags):
            self.assertEqual(rows[tag]['clan_codes'], ['BES'])
            if i >= 44:
                self.assertIsNone(rows[tag]['member_id'])
                continue
            profile = profiles[100 + i]
            self.assertEqual(profile['membership_status'], 'present' if i < 43 else 'not_in_guild')
            self.assertEqual(profile['home_role_checks'], [{'clan_code': 'BES', 'role_id': home_id,
                'has_role': i != 42 if i < 43 else None}])
        # Both projections retain configured role evidence and exclude cosmetic roles.
        retained = json.loads(flow.context.state.reports[flow.completed_results['accounts']['report_id']].payload)
        self.assertEqual({profile['member_id']: profile['roles'] for profile in retained['summary']['members']},
                         {profile['member_id']: profile['roles'] for profile in evidence['accounts']['members']})
        for profile in retained['summary']['members']:
            expected = [home_id] if profile['member_id'] < 142 else []
            self.assertEqual([role['role_id'] for role in profile['roles']], expected)
        self.assertNotIn('roles', retained['rows'][0])

    async def test_all_five_tab_leaders_reach_writer_when_score_rows_are_paged(self):
        @dataclass
        class Score:
            player_tag: str
            player_name: str
            rank: int
            ass_score: float
            detail: str

        @dataclass
        class Snapshot:
            rows: tuple
            profile_key: str

        codes = ['BES', 'BE1', 'BEM', 'BEC', 'BEE']
        def score(clan_code, war_ids):
            return Snapshot(tuple(Score('#P' + str(i), clan_code + ' account ' + str(i),
                i + 1, 50.0 - i, 'Source field ' * 10) for i in range(30)), clan_code)

        context = replace(_context(), cwl_queries=SimpleNamespace(ass_wars=score))
        tool = next(t for t in war_reads.TOOLS if t.definition.name == 'read_cwl_results')
        with patch.object(war_reads, 'selected_wars', AsyncMock(return_value={'war_ids': ['war']})):
            flow, evidence = await self.writer(context, {'read_cwl_results': tool}, [{
                'id': 'ass', 'capability': 'read_cwl_results', 'arguments': {'metric': 'ass',
                    'clan_codes': codes, 'season': '2026-10', 'delivery': 'xlsx',
                    'sheet_by': 'clan_code', 'limit': 16},
                'depends_on': []}])
        result = evidence['ass']
        self.assertLess(len(flow.completed_results['ass']['rows']), 150)
        self.assertEqual(len(result['rows']), 150)
        self.assertEqual({(row['clan_code'], row['player_tag']) for row in result['rows']},
                         {(code, '#P' + str(i)) for code in codes for i in range(30)})
        self.assertEqual(result['artifact_status'], 'complete')
        self.assertEqual([r['clan_code'] for r in result['clan_results']], codes)
        for summary in result['clan_results']:
            self.assertEqual([r['rank'] for r in summary['top_rows']], [1, 2, 3])
            self.assertEqual(summary['top_rows'][0]['player_name'], summary['clan_code'] + ' account 0')
            self.assertEqual(summary['top_rows'][0]['ass_score'], 50.0)
        self.assertEqual(len(flow.context.state.attachments), 1)
        manifest = json.loads(self.last_writer['prompt'].split('\nAvailable evidence:\n', 1)[0])['delivery']
        self.assertEqual(manifest, {'files': [{'filename': 'cwl_results.xlsx', 'delivery': 'attachment',
            'total_rows': 150, 'sheet_count': 5,
            'sheets': [{'name': code, 'rows': 30} for code in codes]}]})
        # Exercise the same attachment and send assembly used by the Discord reply.
        context = replace(flow.context, bot=SimpleNamespace(user=SimpleNamespace(id=999)))
        message = context.source_message
        message.author, message.guild, message.mentions = context.member, context.guild, ()
        message.archive_reply = False
        sent = SimpleNamespace(id=72)
        captured = []

        async def send(text, **options):
            captured.extend((item.filename, item.fp.read()) for item in options.get('files', ()))
            return sent

        message.reply = AsyncMock(side_effect=send)
        surface = AgentDeliveryMixin()
        surface.bot = context.bot
        delivery = AgentDelivery()
        with patch('elbow_helper.agent.delivery.assert_answer_scope', AsyncMock()):
            await surface.send_response(message, 'Synthetic workbook summary.', None,
                context.state.attachments, context=context, delivery=delivery)
        self.assertTrue(delivery.complete)
        self.assertEqual(captured[0][0], manifest['files'][0]['filename'])
        workbook = load_workbook(BytesIO(captured[0][1]), read_only=True)
        try:
            self.assertEqual(workbook.sheetnames, codes)
            for sheet in workbook:
                self.assertEqual(len(list(sheet.values)) - 1, 30)
        finally:
            workbook.close()

    async def test_writer_delivery_tracks_csv_fallback_and_failed_exports(self):
        for name in ('read_sample', 'read_cwl_results'):
            for failed in (False, True):
                with self.subTest(name=name, failed=failed):
                    tool = family_tool(name, 'Read selected rows.', {},
                        AsyncMock(return_value={'rows': [{'note': 'Source row' if failed else 'x' * 33000}]}))
                    steps = [{'id': 'sample', 'capability': name,
                              'arguments': {'delivery': 'xlsx'}, 'depends_on': []}]
                    if failed:
                        with patch('elbow_helper.agent.families.core.export_report',
                                   side_effect=ValueError('Synthetic export failure')):
                            flow, evidence = await self.writer(_context(), {name: tool}, steps)
                    else:
                        flow, evidence = await self.writer(_context(), {name: tool}, steps)
                    manifest = json.loads(self.last_writer['prompt'].split('\nAvailable evidence:\n', 1)[0])['delivery']
                    self.assertEqual(manifest['files'], [] if failed else [{
                        'filename': name.removeprefix('read_') + '.csv',
                        'delivery': 'attachment', 'total_rows': 1}])
                    self.assertEqual(len(flow.context.state.attachments), 0 if failed else 1)
                    self.assertEqual(evidence['sample']['artifact_status'],
                                     'unavailable' if failed else 'complete')
                    if failed:
                        self.assertIsNone(evidence['sample']['artifact'])
                    else:
                        self.assertIn('XLSX cell limit', evidence['sample']['artifact_note'])

    async def test_role_holders_keep_bots_duplicate_names_and_requester(self):
        context = _context()
        role_id = CLANS['BEH'].leadership_role_id
        role = SimpleNamespace(id=role_id, name='Leadership BEH', is_default=lambda: False)
        people = [SimpleNamespace(id=i, display_name='Same name', roles=[role],
            bot=i == 43, joined_at=None, nick=None) for i in (41, 42, 43, 44)]
        context.guild.members = people
        tool = next(t for t in identity_reads.TOOLS if t.definition.name == 'read_member_cohort')
        _, evidence = await self.writer(context, {'read_member_cohort': tool}, [{
            'id': 'holders', 'capability': 'read_member_cohort', 'arguments': {
                'basis': 'role_holders', 'clan_codes': ['BEH']}, 'depends_on': []}])
        self.assertEqual({r['member_id'] for r in evidence['holders']['rows']}, {41, 42, 43, 44})

    async def test_two_role_room_comparison_reaches_writer_with_policy(self):
        context = _context()
        roles = [SimpleNamespace(id=12, name='Co-Leader', permissions=discord.Permissions.none()),
                 SimpleNamespace(id=13, name='Cross-Leader', permissions=discord.Permissions.none())]
        for role in context.guild.roles:
            role.name = 'Fixture role'
        context.guild.roles.extend(roles)
        context.guild.get_role = lambda key: next((r for r in roles if r.id == key), None)
        channel = context.source_message.channel
        channel.name = 'Synthetic room'
        channel.permissions_for = lambda actor: discord.Permissions(
            view_channel=actor.id != 13, read_message_history=actor.id != 13)
        reference = family_tool('read_community_reference', 'Read selected policy.', {},
            AsyncMock(return_value={'rows': [{'title': 'Leadership policy', 'body': 'Synthetic policy.'}]}))
        _, evidence = await self.writer(context, {'read_discord_access': ACCESS_TOOLS[0],
            'read_community_reference': reference}, [
            {'id': 'policy', 'capability': 'read_community_reference', 'arguments': {}, 'depends_on': []},
            {'id': 'access', 'capability': 'read_discord_access', 'arguments': {
                'role_names': ['Co-Leader', 'Cross-Leader']}, 'depends_on': []}])
        rows = {r['actor_name']: r for r in evidence['access']['rows']}
        self.assertTrue(rows['Co-Leader']['view_channel'])
        self.assertFalse(rows['Cross-Leader']['view_channel'])
        self.assertTrue(all(r['channel_id'] == channel.id and r['role_only'] for r in rows.values()))
        self.assertIn('Role-only access does not establish', evidence['access']['coverage'])
        self.assertEqual(evidence['policy']['rows'][0]['body'], 'Synthetic policy.')
