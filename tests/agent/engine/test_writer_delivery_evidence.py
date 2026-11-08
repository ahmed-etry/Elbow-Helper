"""Complete typed comparisons and tab summaries survive the real writer boundary."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan
from tests.agent.engine.test_writer_tables import expand as unpack


class WriterModel(_Model):
    def __init__(self):
        super().__init__(_Session([], []))
        self.writer = None

    def create_agent_session(self, **kwargs):
        if not kwargs['tools']:
            self.writer = kwargs
            return _Session([], [])
        return super().create_agent_session(**kwargs)


class WriterDeliveryEvidenceTests(unittest.IsolatedAsyncioTestCase):
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


    async def test_writer_delivery_tracks_csv_fallback_and_failed_exports(self):
        for name in ('read_sample', 'read_cwl_results'):
            for failed in (False, True):
                with self.subTest(name=name, failed=failed):
                    tool = family_tool(name, 'Read selected rows.', {},
                        AsyncMock(return_value={'rows': [{'note': 'Source row' if failed else 'x' * 33000}]}))
                    steps = [{'id': 'sample', 'capability': name,
                              'arguments': {'delivery': 'xlsx'}, 'depends_on': []}]
                    if failed:
                        with patch('elbow_helper.agent.reads.snapshots.export_report',
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


class ChannelWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_receives_current_directory_and_omits_inaccessible_channels(self):
        context, model = _context(), WriterModel()
        source = context.source_message.channel
        source.name = 'current-room'
        context.guild.channels = (source, SimpleNamespace(id=92, name='base-designs'),
                                  SimpleNamespace(id=93, name='private-bases'))
        flow, _ = AgentService(model)._start_flow(question='Where can I share bases?',
            local_context='', context=context, conversation_history='')
        with patch('elbow_helper.agent.research.directory.can_access_message_channel',
                   AsyncMock(side_effect=lambda context, channel, **kw: channel.id != 93)):
            self.assertTrue(await flow._prepare_answer_session())
        evidence = json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1])['results']
        self.assertEqual(evidence['channel_directory']['channels'],
                         {'91': 'current-room', '92': 'base-designs'})
        self.assertIn('Mention or recommend only channels present there', model.writer['system_prompt'])
        self.assertNotIn('private-bases', model.writer['prompt'])
        self.assertFalse(context.state.proposed_changes)
