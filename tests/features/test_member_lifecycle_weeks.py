import unittest
from datetime import datetime, timezone

from elbow_helper.features.member_lifecycle.weeks import month_bounds, ordinal, week_label, week_start


class RecruitmentWeekTests(unittest.TestCase):
    def test_week_runs_monday_to_sunday_in_utc(self):
        monday = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.assertEqual(week_start(datetime(2026, 11, 22, 23, 59, 59, tzinfo=timezone.utc)), monday)
        self.assertEqual(week_start(datetime(2026, 11, 23, tzinfo=timezone.utc)), datetime(2026, 11, 23, tzinfo=timezone.utc))

    def test_week_across_two_months_belongs_to_the_month_with_more_of_its_days(self):
        # 30 Nov to 6 Dec 2026 has one November day and six December days.
        self.assertEqual(week_label(week_start(datetime(2026, 11, 30, tzinfo=timezone.utc))), "Week 1 · December 2026")
        self.assertEqual(week_label(week_start(datetime(2026, 12, 7, tzinfo=timezone.utc))), "Week 2 · December 2026")
        # 26 Oct to 1 Nov 2026 has six October days.
        self.assertEqual(week_label(week_start(datetime(2026, 11, 1, tzinfo=timezone.utc))), "Week 5 · October 2026")
        self.assertEqual(week_label(week_start(datetime(2026, 11, 2, tzinfo=timezone.utc))), "Week 1 · November 2026")

    def test_month_bounds_are_the_calendar_month(self):
        utc = timezone.utc
        self.assertEqual(month_bounds(datetime(2026, 11, 30, 23, 59, tzinfo=utc)),
                         (datetime(2026, 11, 1, tzinfo=utc), datetime(2026, 12, 1, tzinfo=utc)))
        self.assertEqual(month_bounds(datetime(2026, 12, 31, tzinfo=utc)),
                         (datetime(2026, 12, 1, tzinfo=utc), datetime(2027, 1, 1, tzinfo=utc)))

    def test_ordinals(self):
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st", "111th"])
