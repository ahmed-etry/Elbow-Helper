"""The participation tool describes when a period is required."""
import unittest

from elbow_helper.agent.reads.war_tools import TOOLS


class ParticipationPeriodNoteTests(unittest.TestCase):
    def test_usage_note_names_the_required_period_choices(self):
        note = next(tool for tool in TOOLS if tool.definition.name == 'read_war_participation').definition.usage_note
        self.assertIn('Without accounts, members or a cohort', note)
        self.assertIn('otherwise all their stored wars are read', note)
        self.assertLessEqual(len(note), 160)
        for choice in ('season', 'season_keys', 'latest_completed', 'after/before', 'war_ids'):
            self.assertIn(choice, note)


if __name__ == '__main__':
    unittest.main()
