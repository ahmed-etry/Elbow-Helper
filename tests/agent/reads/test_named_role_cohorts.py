"""Named responsibilities never fall back to configured clan leadership."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.plan.input_rules import input_rule_issue
from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads.identity_reads import member_cohort
from tests.agent.request_fixtures import flow_context as _context

TOOL = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_cohort')
OCTOBER = '2026-10-01T00:00:00+00:00'


class NamedRoleTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = _context()
        def role(identifier, name):
            return SimpleNamespace(id=identifier, name=name, is_default=lambda: False)
        war = role(27, 'War Co')
        home = role(CLANS['BEH'].member_role_id, 'BEH')
        lead = role(CLANS['BEH'].leadership_role_id, 'Leadership BEH')
        participant = role(CLANS['BEH'].war_role_id, 'War BEH')
        members = [SimpleNamespace(id=i, display_name=f'Member {i}', joined_at=None, roles=held)
                   for i, held in ((101, [war, home]), (102, [war]),
                                   (103, [lead, home]), (104, [participant, home]))]
        context.guild.roles = [war, home, lead, participant]
        context.guild.members = [context.member, *members]
        context.guild.get_member = lambda identifier: next(
            (m for m in context.guild.members if m.id == identifier), None)
        return context

    async def test_named_role_returns_all_holders_without_leadership_or_war_participants(self):
        result = await member_cohort(self.context(), {'basis': 'role_holders', 'role_name': 'War Co'})
        self.assertEqual({r['member_id'] for r in result['rows']}, {101, 102})
        self.assertEqual(result['selected_role_ids'], [27])
        self.assertTrue(all([role['role_id'] for role in row['roles']] == [27]
                            for row in result['rows']))

    async def test_clan_scope_intersects_named_holders_with_home_role(self):
        result = await member_cohort(self.context(), {'basis': 'role_holders',
            'role_name': 'war co', 'clan_codes': ['BEH']})
        self.assertEqual([r['member_id'] for r in result['rows']], [101])
        self.assertEqual(result['selected_role_ids'], [27])
        self.assertEqual(result['clan_member_role_ids'], [CLANS['BEH'].member_role_id])

    async def test_duplicate_and_absent_names_require_resolution_without_fallback(self):
        for duplicate in (False, True):
            context = self.context()
            if duplicate:
                context.guild.roles.append(SimpleNamespace(id=28, name='War Co'))
            result = await member_cohort(context, {'basis': 'role_holders',
                'role_name': 'War Co' if duplicate else 'Absent role', 'clan_codes': ['BEH']})
            self.assertEqual(result['status'], 'needs_input')
            self.assertEqual(result['rows'], [])
            self.assertEqual(len(result['roles']), 2 if duplicate else 0)

    async def test_recovery_names_the_option_and_the_dates_that_were_sent(self):
        result = await member_cohort(self.context(), {'basis': 'role_holders', 'role_name': 'Absent role'})
        self.assertEqual(result['missing_fields'], ['role_ids'])
        self.assertIn('role_ids', TOOL.definition.parameters['properties'])
        for dates, error in ((('accepted_after', '2026-11-01T00:00:00'), 'Choose timezone-aware acceptance dates.'),
                             (('accepted_after', '2026-11-02T00:00:00+00:00', 'accepted_before', '2026-11-01T00:00:00+00:00'),
                              'The acceptance window must end after it starts.')):
            arguments = {'basis': 'accepted_recruitment', **dict(zip(dates[::2], dates[1::2]))}
            with patch('elbow_helper.agent.reads.workflow_reads.active_trial_rows',
                       AsyncMock(return_value={'rows': []})):
                self.assertEqual((await member_cohort(self.context(), arguments))['error'], error)
        self.assertEqual(input_rule_issue({'basis': 'accepted_recruitment', 'accepted_after': '2026-11-01T00:00:00'},
                                          TOOL.definition.parameters)[0], 'Choose timezone-aware acceptance dates.')

    async def test_next_page_of_a_named_role_report_by_its_role_id_reads_that_role(self):
        # Live: the first read chose the role by name, the next page sent the same role's ID.
        context = self.context()
        first = await TOOL.handler(context, {'basis': 'role_holders', 'role_name': 'War Co',
                                             'clan_codes': ['BEH'], 'include_roles': True})
        again = await TOOL.handler(context, {'basis': 'role_holders', 'role_ids': [27], 'clan_codes': ['BEH'],
                                             'report_id': first['report_id'], 'offset': 0, 'limit': 100})
        self.assertNotIn('error', again)
        self.assertEqual([row['member_id'] for row in again['rows']], [row['member_id'] for row in first['rows']])
        self.assertEqual((again['selection']['role_ids'], again['selection']['include_roles']), ([27], True))
        self.assertNotIn('role_name', again['selection'])

    async def test_each_filter_is_read_by_its_basis_or_refused(self):
        context = self.context()
        joined = {member.id: datetime(2026, 10, day, tzinfo=timezone.utc)
                  for member, day in zip(context.guild.members[1:], (3, 1, 4, 2))}
        for member in context.guild.members[1:]:
            member.joined_at = joined[member.id]
        # Join and trial cohorts keep only the holders of the chosen roles.
        with patch.object(identity_reads, 'member_accounts', AsyncMock(return_value={'rows': []})):
            result = await member_cohort(context, {'basis': 'joined_discord', 'role_ids': [27]})
        self.assertEqual([row['member_id'] for row in result['rows']], [102, 101])
        trials = {'rows': [{'applicant_member_id': 101, 'start_at': '2026-11-01T00:00:00+00:00'},
                           {'applicant_member_id': 103, 'start_at': '2026-11-02T00:00:00+00:00'}]}
        with patch('elbow_helper.agent.reads.workflow_reads.active_trial_rows', AsyncMock(return_value=trials)):
            result = await member_cohort(context, {'basis': 'accepted_recruitment', 'role_ids': [27]})
        self.assertEqual([row['member_id'] for row in result['rows']], [101])
        # A clan roster cohort is put in Discord join order when asked.
        accounts = [{'clan_code': 'BEH', 'player_tag': '#P' + str(identifier)} for identifier in (101, 102, 103, 104)]
        context = replace(context, account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {'discord_user_id': int(tag[2:]), 'player_tag': tag} for tag in tags}))
        roster = {'rows': accounts, 'count_complete': True, 'clans': []}
        with patch.object(identity_reads, 'clan_rosters', AsyncMock(return_value=roster)):
            result = await member_cohort(context, {'basis': 'clan_rosters', 'clan_codes': ['BEH'],
                                                   'sort': 'newest_first'})
        self.assertEqual([row['member_id'] for row in result['rows']], [103, 101, 104, 102])
        # Options the basis never reads are refused before any read, naming the basis that reads them.
        accepted = 'accepted_after and accepted_before require basis=accepted_recruitment.'
        for arguments, error in (
                ({'basis': 'role_holders', 'clan_codes': ['BEH'], 'accepted_after': OCTOBER}, accepted),
                ({'basis': 'joined_discord', 'accepted_before': OCTOBER}, accepted),
                ({'basis': 'clan_rosters', 'accepted_after': OCTOBER}, accepted),
                ({'basis': 'clan_rosters', 'joined_after': OCTOBER},
                 'joined_after and joined_before require basis=joined_discord or role_holders.'),
                ({'basis': 'clan_rosters', 'role_ids': [27]},
                 'role_ids requires basis=role_holders, joined_discord or accepted_recruitment.'),
                ({'basis': 'clan_rosters', 'member_ids': [101]}, 'Use basis=selected for a chosen set.'),
                ({'basis': 'clan_rosters', 'cohort_ref': 'a' * 32}, 'Use basis=selected for a chosen set.')):
            with self.subTest(arguments=arguments):
                self.assertEqual(input_rule_issue(arguments, TOOL.definition.parameters)[0], error)
