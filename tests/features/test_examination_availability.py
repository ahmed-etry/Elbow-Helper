"""Typed examiner availability reads a start without am/pm against the end."""

import unittest

from elbow_helper.features.examination.availability.logic import parse_availability_windows


class AvailabilityRangeTests(unittest.TestCase):
    def test_start_without_am_pm_is_read_against_the_end(self):
        # "Daily 9-5pm" was saved as 21:00-17:00.
        for text, window in (("Daily 9-5pm", (9, 17)), ("Daily 10-10pm", (10, 22)),
                             ("Mon-Fri 13:00-5pm", (13, 17)), ("Daily 8-11pm", (20, 23)),
                             ("Sat-Sun 12-6pm", (12, 18)), ("9am-5pm", (9, 17)),
                             ("Daily 09:00-17:00", (9, 17))):
            with self.subTest(text=text):
                self.assertEqual([times for _, times in parse_availability_windows(text)], [window])
