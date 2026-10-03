"""Roster setup validation and replies belong to the feature."""

from types import SimpleNamespace
import unittest

from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.config import MAX_ROSTER_MEMBERS
from elbow_helper.features.rosters import settings


class RosterSetupValidationTests(unittest.TestCase):
    def test_names_limits_and_confirmation_values_use_shared_rules(self):
        self.assertEqual(settings.validate_roster_setup(
            name=" War  Signup ", max_members=MAX_ROSTER_MEMBERS, min_townhall=0,
        ), {"name": "War Signup", "max_members": MAX_ROSTER_MEMBERS, "min_townhall": 0})
        for name in ("", "x" * 101):
            with self.subTest(name=name), self.assertRaises(ValueError):
                settings.validate_roster_setup(name=name)
        roster, source = SimpleNamespace(name="War"), SimpleNamespace(name="Source")
        for operation, expected in (
            ("create", "Created **War**."), ("clone", "Created **War** from **Source**."),
            ("delete", "Deleted **War**."), ("edit", "Updated **War**."),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(settings.roster_setup_confirmation(operation, roster, source=source), expected)

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
