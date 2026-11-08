"""Cohort and recruitment rows follow member lookups: profile, roles and history only on request."""
import unittest

from elbow_helper.agent.tool_support.model_view import family_model_view

BASIS = 'Discord role assignment, not a main account location'
ACCOUNT = {'player_tag': '#P0', 'player_name': 'Synthetic', 'townHallLevel': 12, 'clan': {'tag': '#C0', 'name': 'Clan'},
           'clan_status': 'in_clan', 'donations': 40, 'war_stars': 900, 'ranked_league': {'id': 1, 'name': 'League'},
           'role': 'member', 'observed_at': '2026-10-01T00:00:00+00:00', 'profile_status': 'ok',
           'record_history': [{'kind': 'owner', 'member_id': 9}]}


def member_row(**extra):
    return {'member_id': 12, 'member_name': 'Synthetic member', 'membership_status': 'present',
            'nickname': 'Nick', 'joined_at': '2026-01-01T00:00:00+00:00',
            'family_membership_roles': {'member': {'role_id': 1, 'held': True}, 'trial': {'role_id': 2, 'held': False}},
            'home_clans': [{'clan_code': 'BEH', 'name': 'Home', 'tag': '#H0', 'role_id': 3, 'role_name': 'BEH'}],
            'participation_clans': [], 'home_clan_status': 'resolved', 'home_clan_basis': BASIS,
            'roles': [{'role_id': 5, 'name': 'Synthetic role'}], 'selection_status': 'selected',
            'accounts': [dict(ACCOUNT)], **extra}


def cohort_view(selection, **extra):
    return family_model_view({'family': 'read_member_cohort', 'basis': selection.get('basis'),
                              'selection': selection, 'rows': [member_row()], **extra})


class CohortProfileTests(unittest.TestCase):

    def test_include_profile_roles_and_history_bring_their_fields_back(self):
        row = cohort_view({'basis': 'selected', 'include_profile': True, 'include_standing': True,
                           'include_roles': True, 'include_history': True})['rows'][0]
        for field in ('nickname', 'joined_at', 'family_membership_roles', 'home_clans', 'home_clan_status', 'roles'):
            self.assertIn(field, row)
        self.assertNotIn('home_clan_basis', row)
        account = row['accounts'][0]
        for field in ('donations', 'war_stars', 'ranked_league', 'role', 'observed_at', 'record_history'):
            self.assertIn(field, account)

    def test_profile_and_standing_are_separate_requests(self):
        profile = cohort_view({'basis': 'selected', 'include_profile': True})['rows'][0]
        for field in ('nickname', 'joined_at', 'family_membership_roles', 'home_clans', 'home_clan_status'):
            self.assertIn(field, profile)
        self.assertEqual((profile['accounts'][0]['role'], profile['accounts'][0]['observed_at']),
                         ('member', '2026-10-01T00:00:00+00:00'))
        for field in ('donations', 'war_stars', 'ranked_league'):
            self.assertNotIn(field, profile['accounts'][0])
        standing = cohort_view({'basis': 'selected', 'include_standing': True})['rows'][0]
        for field in ('nickname', 'joined_at', 'family_membership_roles', 'home_clans', 'home_clan_status'):
            self.assertNotIn(field, standing)
        self.assertNotIn('role', standing['accounts'][0])
        self.assertEqual((standing['accounts'][0]['donations'], standing['accounts'][0]['war_stars']), (40, 900))
        self.assertEqual(standing['accounts'][0]['ranked_league'], {'id': 1, 'name': 'League'})

    def test_a_cohort_keeps_the_fields_its_selection_is_made_of(self):
        joined = cohort_view({'basis': 'joined_discord', 'joined_before': '2026-05-01T00:00:00+00:00'})['rows'][0]
        self.assertIn('joined_at', joined)
        self.assertIn('family_membership_roles', joined)
        self.assertNotIn('home_clans', joined)
        by_clan = cohort_view({'basis': 'role_holders', 'role_name': 'Synthetic role', 'clan_codes': ['BEH'],
                               'include_roles': False})['rows'][0]
        self.assertIn('home_clans', by_clan)
        self.assertIn('home_clan_status', by_clan)
        self.assertNotIn('joined_at', by_clan)
        self.assertIn('roles', by_clan)  # requested role holders show the roles they hold
        roster = cohort_view({'basis': 'clan_rosters'})['rows'][0]
        self.assertIn('home_clans', roster)
        self.assertEqual(roster['accounts'][0]['donations'], 40)


class RecruitmentSnapshotTests(unittest.TestCase):
    def view(self, selection):
        return family_model_view({'family': 'read_recruitment_pipeline', 'selection': selection,
            'rows': [{'applicant_member_id': 12, 'ticket_channel_id': 7, 'accounts': [dict(ACCOUNT)]}],
            'members': [{'member_id': 12, 'member_name': 'Synthetic member', 'account_tags': ['#P0'],
                         'home_clan_status': 'resolved', 'roles': []}]})


    def test_applicant_profiles_and_rows_are_not_dropped(self):
        view = self.view({'stage': 'ticket_evidence', 'include_accounts': True})
        self.assertEqual(len(view['rows']), 1)
        self.assertEqual(view['rows'][0]['ticket_channel_id'], 7)
        self.assertEqual(view['members'][0]['home_clan_status'], 'resolved')


if __name__ == '__main__':
    unittest.main()
