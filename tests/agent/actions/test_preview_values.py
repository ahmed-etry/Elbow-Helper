"""Preview values retain zero and distinguish missing values from visibility."""

import unittest

from elbow_helper.agent.actions.values import display_value


class PreviewValueTests(unittest.TestCase):
    def test_missing_zero_lists_and_boolean_states_remain_distinct(self):
        for value in (None, "", [], ()):
            with self.subTest(value=value):
                self.assertEqual(display_value(value), "Not set")
        self.assertEqual(display_value(0), "0")
        self.assertEqual(display_value([11, 12]), "11, 12")
        self.assertEqual(display_value(False), "No")
        self.assertEqual(display_value(True), "Yes")
        self.assertEqual(display_value(False, visibility=True), "Hidden")
        self.assertEqual(display_value(True, visibility=True), "Visible")
