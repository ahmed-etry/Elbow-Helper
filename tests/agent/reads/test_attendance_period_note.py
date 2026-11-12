"""The attendance tool tells the planner up front that a period is required."""
import unittest

from elbow_helper.agent.reads.war_tools import TOOLS


class AttendancePeriodNoteTests(unittest.TestCase):
    def test_usage_note_names_the_required_period_choices(self):
        note = next(tool for tool in TOOLS if tool.definition.name == 'read_war_attendance').definition.usage_note
        self.assertIn('A period is required, with no default', note)
        self.assertLessEqual(len(note), 160)
        for choice in ('season', 'season_keys', 'latest_completed', 'after/before', 'war_ids'):
            self.assertIn(choice, note)


if __name__ == '__main__':
    unittest.main()
