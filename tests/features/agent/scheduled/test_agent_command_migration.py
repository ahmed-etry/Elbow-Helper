"""Saved action scopes keep working when capability names change."""
import json
import unittest

from elbow_helper.features.agent.commands.registry import legacy_capability_names
from elbow_helper.features.agent.scheduled.store import ScheduledStore


class CommandMigrationTests(unittest.TestCase):
    def test_stored_action_names_are_mapped_on_load(self):
        names = legacy_capability_names()
        self.assertEqual(names["run_command_accept"], "accept_applicant")
        record = ScheduledStore._standing_record({"rule_json": json.dumps({"allowed_actions": [
            {"capability": "run_command_roster_create", "fixed_values": {"name": "Synthetic"}},
        ]})})
        self.assertEqual(record["rule"]["allowed_actions"][0]["capability"], "roster_create")
        self.assertEqual(
            record["rule"]["allowed_actions"][0]["fixed_values"], {"name": "Synthetic"},
        )
