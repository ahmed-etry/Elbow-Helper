"""Roster setup validation and replies belong to the feature."""

from types import SimpleNamespace
import unittest

from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.config import MAX_ROSTER_MEMBERS


class RosterSetupValidationTests(unittest.TestCase):

    def test_edit_rejects_limits_outside_the_feature_bounds(self):
        for values in ({"max_members": 0}, {"max_members": MAX_ROSTER_MEMBERS + 1},
                       {"min_townhall": -1}):
            with self.subTest(values=values):
                changes, issue = Rosters.roster_edit_changes(
                    SimpleNamespace(), name=None, clan_code=None, role_id=None,
                    max_members=values.get("max_members"), min_townhall=values.get("min_townhall"),
                    remove_signup_role=False,
                )
                self.assertEqual(changes, {})
                self.assertIsNotNone(issue)
